#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import bisect
import logging
import os
from hashlib import sha1
from io import BytesIO
from typing import Optional

import pyrogram
from pyrogram import raw
from pyrogram.connection import Connection
from pyrogram.crypto import mtproto
from pyrogram.errors import (
    AuthKeyDuplicated,
    BadMsgNotification,
    FloodPremiumWait,
    FloodWait,
    InternalServerError,
    PersistentTimestampOutdated,
    RPCError,
    SecurityCheckMismatch,
    ServiceUnavailable,
    Unauthorized,
)
from pyrogram.raw.all import layer
from pyrogram.raw.core import FutureSalts, Int, MsgContainer, TLObject
from ..helpers import log_task_exception

from .internals import MsgFactory, MsgId

log = logging.getLogger(__name__)

# Sentinel object to distinguish restart interruption from timeout
RESTART_SENTINEL = object()


class SessionRestartedError(Exception):
    """Raised when a request is interrupted by session restart.
    
    This typically happens during session restarts and the request
    should be retried with a new session. This error helps distinguish
    between network timeouts and restart-induced interruptions.
    """
    pass


class Result:
    def __init__(self):
        self.value = None
        self.event = asyncio.Event()


class Session:
    START_TIMEOUT = 2
    WAIT_TIMEOUT = 15
    SLEEP_THRESHOLD = 10
    MAX_RETRIES = 10
    ACKS_THRESHOLD = 10
    PING_INTERVAL = 5
    STORED_MSG_IDS_MAX_SIZE = 1000 * 2

    TRANSPORT_ERRORS = {
        404: "auth key not found",
        429: "transport flood",
        444: "invalid DC"
    }

    @staticmethod
    def _log_task_exception(task: asyncio.Task):
        """Log any unhandled exception raised by an asyncio.Task."""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as err:  # pragma: no cover
            log.exception("Error while retrieving task exception: %s", err)
            return
        if exc is not None:
            log.exception("Unhandled exception in background task", exc_info=exc)

    def __init__(
        self,
        client: "pyrogram.Client",
        dc_id: int,
        auth_key: bytes,
        test_mode: bool,
        is_media: bool = False,
        is_cdn: bool = False
    ):
        self.client = client
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.test_mode = test_mode
        self.is_media = is_media
        self.is_cdn = is_cdn

        self.connection: Optional[Connection] = None

        self.auth_key_id = sha1(auth_key).digest()[-8:]

        self.session_id = os.urandom(8)
        self.msg_factory = MsgFactory()

        self.salt = 0

        self.pending_acks = set()

        self.results = {}

        self.stored_msg_ids = []

        self.ping_task = None
        self.ping_task_event = asyncio.Event()

        self.recv_task = None

        self.is_started = asyncio.Event()
        self.restart_event = asyncio.Event()

    async def start(self):
        while True:
            self.connection = self.client.connection_factory(
                dc_id=self.dc_id,
                test_mode=self.test_mode,
                ipv6=self.client.ipv6,
                proxy=self.client.proxy,
                media=self.is_media,
                protocol_factory=self.client.protocol_factory,
                loop=self.client.loop
            )

            try:
                await self.connection.connect()

                if self.recv_task is None or self.recv_task.done():
                    self.recv_task = self.client.loop.create_task(self.recv_worker())

                await self.send(raw.functions.Ping(ping_id=0), timeout=self.START_TIMEOUT)

                if not self.is_cdn:
                    await self.send(
                        raw.functions.InvokeWithLayer(
                            layer=layer,
                            query=raw.functions.InitConnection(
                                api_id=await self.client.storage.api_id(),
                                app_version=self.client.app_version,
                                device_model=self.client.device_model,
                                system_version=self.client.system_version,
                                system_lang_code=self.client.system_lang_code,
                                lang_pack=self.client.lang_pack,
                                lang_code=self.client.lang_code,
                                query=raw.functions.help.GetConfig(),
                                params=self.client.init_connection_params,
                            )
                        ),
                        timeout=self.START_TIMEOUT
                    )

                self.ping_task = self.client.loop.create_task(self.ping_worker())

                log.info("Session initialized: Pyrogram v%s (Layer %s)", pyrogram.__version__, layer)
                log.info("Device: %s - %s", self.client.device_model, self.client.app_version)
                log.info("System: %s (%s)", self.client.system_version, self.client.lang_code)
            except (AuthKeyDuplicated, Unauthorized) as e:
                await self.stop()
                raise e
            except ConnectionError as e:
                await self.stop()
                # raise e
            except (OSError, RPCError):
                await self.stop()
            except Exception as e:
                await self.stop()
                raise e
            else:
                break

        self.is_started.set()

        log.info("Session started")

        if callable(self.client.connect_handler):
            try:
                await self.client.connect_handler(self.client, self)
            except Exception as e:
                log.exception(e)

    async def stop(self):
        if callable(self.client.disconnect_handler):
            try:
                await self.client.disconnect_handler(self.client, self)
            except Exception as e:
                log.exception(e)

        self.is_started.clear()

        self.stored_msg_ids.clear()

        self.ping_task_event.set()

        if self.ping_task is not None:
            await self.ping_task

        self.ping_task_event.clear()

        await self.connection.close()

        if self.recv_task:
            try:
                self.recv_task.cancel()
                await self.recv_task
            except asyncio.CancelledError:
                pass

            self.recv_task = None

        log.info("Session stopped")

    async def restart(self):
        if self.restart_event.is_set():
            return  # Another restart is already in progress
        self.restart_event.set()

        try:
            await self.stop()

            # Atomic notification and clearing to avoid race conditions
            # Take snapshot of pending results first
            pending_results = list(self.results.items())
            
            # Clear collections atomically
            self.pending_acks.clear()
            self.results.clear()

            # Notify all pending requests after clearing to avoid KeyError
            for msg_id, result in pending_results:
                if not result.event.is_set():
                    result.value = RESTART_SENTINEL  # Use sentinel to distinguish from timeout
                    result.event.set()

            # Generate new session ID to ensure fresh session state
            self.session_id = os.urandom(8)

            await self.start()
        finally:
            self.restart_event.clear()

    def safe_restart(self):
        """Safely restart session avoiding concurrent restarts"""
        if not self.restart_event.is_set():
            task = self.client.loop.create_task(self.restart())
            task.add_done_callback(self._log_task_exception)

    async def handle_packet(self, packet):
        try:
            data = await self.client.loop.run_in_executor(
                pyrogram.crypto_executor,
                mtproto.unpack,
                BytesIO(packet),
                self.session_id,
                self.auth_key,
                self.auth_key_id
            )
        except ValueError as e:
            log.debug(e)
            self.safe_restart()
            return

        messages = (
            data.body.messages
            if isinstance(data.body, MsgContainer)
            else [data]
        )

        log.debug("Received: %s", data)

        for msg in messages:
            if msg.seq_no % 2 != 0:
                if msg.msg_id in self.pending_acks:
                    continue
                else:
                    self.pending_acks.add(msg.msg_id)

            # Handle NewSessionCreated messages first to avoid security check issues after restart
            if isinstance(msg.body, raw.types.NewSessionCreated):
                # Add basic time sanity check for security
                time_diff = (msg.msg_id - MsgId()) / 2 ** 32
                if abs(time_diff) > 300:  # 5 minutes tolerance for clock skew
                    log.warning("NewSessionCreated with suspicious timing: %s seconds diff", time_diff)
                    # Still process but log the warning
                bisect.insort(self.stored_msg_ids, msg.msg_id)
                continue

            try:
                if len(self.stored_msg_ids) > Session.STORED_MSG_IDS_MAX_SIZE:
                    del self.stored_msg_ids[:Session.STORED_MSG_IDS_MAX_SIZE // 2]

                if self.stored_msg_ids:
                    if msg.msg_id < self.stored_msg_ids[0]:
                        raise SecurityCheckMismatch("The msg_id is lower than all the stored values")

                    if msg.msg_id in self.stored_msg_ids:
                        raise SecurityCheckMismatch("The msg_id is equal to any of the stored values")

                    time_diff = (msg.msg_id - MsgId()) / 2 ** 32

                    if time_diff > 30:
                        raise SecurityCheckMismatch("The msg_id belongs to over 30 seconds in the future. "
                                                    "Most likely the client time has to be synchronized.")

                    if time_diff < -300:
                        raise SecurityCheckMismatch("The msg_id belongs to over 300 seconds in the past. "
                                                    "Most likely the client time has to be synchronized.")
            except SecurityCheckMismatch as e:
                log.info("Discarding packet: %s", e)
                await self.connection.close()
                return
            else:
                bisect.insort(self.stored_msg_ids, msg.msg_id)

            if isinstance(msg.body, (raw.types.MsgDetailedInfo, raw.types.MsgNewDetailedInfo)):
                self.pending_acks.add(msg.body.answer_msg_id)
                continue

            msg_id = None

            if isinstance(msg.body, (raw.types.BadMsgNotification, raw.types.BadServerSalt)):
                msg_id = msg.body.bad_msg_id
            elif isinstance(msg.body, (FutureSalts, raw.types.RpcResult)):
                msg_id = msg.body.req_msg_id
            elif isinstance(msg.body, raw.types.Pong):
                msg_id = msg.body.msg_id
            else:
                if self.client is not None:
                    task = self.client.loop.create_task(self.client.handle_updates(msg.body))
                    task.add_done_callback(log_task_exception)

            if msg_id in self.results:
                self.results[msg_id].value = getattr(msg.body, "result", msg.body)
                self.results[msg_id].event.set()

        if len(self.pending_acks) >= self.ACKS_THRESHOLD:
            log.debug("Sending %s acks", len(self.pending_acks))

            try:
                await self.send(raw.types.MsgsAck(msg_ids=list(self.pending_acks)), False)
            except OSError:
                pass
            else:
                self.pending_acks.clear()

    async def ping_worker(self):
        log.info("PingTask started")

        while True:
            try:
                await asyncio.wait_for(self.ping_task_event.wait(), self.PING_INTERVAL)
            except asyncio.TimeoutError:
                pass
            else:
                break

            try:
                await self.send(
                    raw.functions.PingDelayDisconnect(
                        ping_id=0, disconnect_delay=self.WAIT_TIMEOUT + 10
                    ), False
                )
            except OSError:
                self.safe_restart()
                break
            except RPCError:
                pass

        log.info("PingTask stopped")

    async def recv_worker(self):
        log.info("NetworkTask started")

        while True:
            try:
                packet = await asyncio.wait_for(self.connection.recv(), timeout=1)
            except asyncio.TimeoutError:
                continue

            if packet is None or len(packet) == 4:
                if packet:
                    error_code = -Int.read(BytesIO(packet))

                    if error_code == 404:
                        raise Unauthorized(
                            "Auth key not found in the system. You must delete your session file "
                            "and log in again with your phone number or bot token."
                        )

                    log.warning(
                        "Server sent transport error: %s (%s)",
                        error_code, Session.TRANSPORT_ERRORS.get(error_code, "unknown error")
                    )

                if self.is_started.is_set():
                    self.safe_restart()

                break

            self.client.loop.create_task(self.handle_packet(packet))

        log.info("NetworkTask stopped")

    async def send(self, data: TLObject, wait_response: bool = True, timeout: float = WAIT_TIMEOUT):
        message = self.msg_factory(data)
        msg_id = message.msg_id

        if wait_response:
            self.results[msg_id] = Result()

        log.debug("Sent: %s", message)

        payload = await self.client.loop.run_in_executor(
            pyrogram.crypto_executor,
            mtproto.pack,
            message,
            self.salt,
            self.session_id,
            self.auth_key,
            self.auth_key_id
        )

        try:
            await self.connection.send(payload)
        except OSError as e:
            self.results.pop(msg_id, None)
            raise e

        if wait_response:
            try:
                await asyncio.wait_for(self.results[msg_id].event.wait(), timeout)
            except asyncio.TimeoutError:
                pass

            # Handle case where results might have been cleared during session restart
            result_obj = self.results.pop(msg_id, None)
            if result_obj is None:
                raise SessionRestartedError("Request interrupted by session restart")

            result = result_obj.value

            # Check for restart interruption using sentinel
            if result is RESTART_SENTINEL:
                raise SessionRestartedError("Request interrupted by session restart")

            if result is None:
                raise TimeoutError("Request timed out")

            if isinstance(result, raw.types.RpcError):
                if isinstance(data, (raw.functions.InvokeWithoutUpdates, raw.functions.InvokeWithTakeout)):
                    data = data.query

                RPCError.raise_it(result, type(data))

            if isinstance(result, raw.types.BadMsgNotification):
                log.warning("%s: %s", BadMsgNotification.__name__, BadMsgNotification(result.error_code))

            if isinstance(result, raw.types.BadServerSalt):
                self.salt = result.new_server_salt
                return await self.send(data, wait_response, timeout)

            return result

    async def invoke(
        self,
        query: TLObject,
        retries: int = MAX_RETRIES,
        timeout: float = WAIT_TIMEOUT,
        sleep_threshold: float = SLEEP_THRESHOLD
    ):
        try:
            await asyncio.wait_for(self.is_started.wait(), self.WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        if isinstance(query, (raw.functions.InvokeWithoutUpdates, raw.functions.InvokeWithTakeout)):
            inner_query = query.query
        else:
            inner_query = query

        query_name = ".".join(inner_query.QUALNAME.split(".")[1:])

        attempts_used = 0
        max_attempts = retries + 1  # +1 because retries=10 means 11 total attempts

        while attempts_used < max_attempts:
            try:
                return await self.send(query, timeout=timeout)
            except SessionRestartedError:
                # Add moderate delay before retry on session restart to prevent rapid restart loops
                if attempts_used == retries:  # Last attempt
                    raise TimeoutError("Request failed due to session restart")

                # Fixed progressive backoff: earlier attempts = shorter delays
                delay = min(2.0, 0.5 * (attempts_used + 1))  # 0.5s, 1.0s, 1.5s, 2.0s
                log.warning('[%s] Session restart interrupted "%s" → waiting %ss before retry (%s/%s)',
                            self.client.name, query_name, delay,
                            attempts_used + 1, max_attempts)

                await asyncio.sleep(delay)
                attempts_used += 1
                continue
            except (FloodWait, FloodPremiumWait) as e:
                amount = e.value

                if amount > sleep_threshold >= 0:
                    raise

                log.warning('[%s] Waiting for %s seconds before continuing (required by "%s")',
                            self.client.name, amount, query_name)

                await asyncio.sleep(amount)
                # Don't increment attempts_used for FloodWait - this is not a "real" retry
                continue
            except (OSError, InternalServerError, ServiceUnavailable) as e:
                if attempts_used == retries:  # Last attempt
                    raise e from None

                # Fix logging to use proper attempt counting
                (log.warning if attempts_used >= retries - 2 else log.info)(
                    '[%s] Retrying "%s" due to: %s (attempt %s/%s)',
                    self.client.name, query_name, str(e) or repr(e),
                    attempts_used + 1, max_attempts
                )

                # Only restart session for network errors (OSError) or certain server errors
                # Don't restart for temporary server issues like PERSISTENT_TIMESTAMP_OUTDATED
                should_restart = isinstance(e, OSError) or (
                    isinstance(e, (InternalServerError, ServiceUnavailable)) and
                    not isinstance(e, PersistentTimestampOutdated)
                )

                if should_restart:
                    if not self.restart_event.is_set():
                        self.safe_restart()
                    else:
                        # multiple Exceptions can be raised in a row, so we need to wait for the restart to finish
                        try:
                            await asyncio.wait_for(self.restart_event.wait(), self.WAIT_TIMEOUT)
                        except asyncio.TimeoutError:
                            pass

                await asyncio.sleep(0.5)
                attempts_used += 1
                continue
            except TimeoutError as e:
                # Continuous timeouts likely mean the connection is broken. Attempt a full session restart
                # before retrying (bounded by the remaining retries).
                if attempts_used == retries:  # Last attempt
                    raise e

                log.warning('[%s] Timeout while executing "%s" → restarting session and retrying (attempt %s/%s)',
                            self.client.name, query_name, attempts_used + 1, max_attempts)

                try:
                    await self.restart()
                except Exception as err:  # pragma: no cover
                    log.exception('Error while restarting session after timeout: %s', err)

                # Give the session a brief moment to settle
                await asyncio.sleep(0.5)
                attempts_used += 1
                continue

        # This should never be reached, but just in case
        raise RuntimeError(f"Unexpected exit from retry loop after {attempts_used} attempts")

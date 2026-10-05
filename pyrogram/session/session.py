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

from __future__ import annotations as _annotations

import asyncio
import bisect
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum, auto
from hashlib import sha1, sha256
from io import BytesIO
from typing import TYPE_CHECKING, Any

import pyrogram
from pyrogram import raw, utils
from pyrogram.connection.proxy import client_proxy_address
from pyrogram.crypto import mtproto
from pyrogram.errors import (
    AuthKeyDuplicated,
    BadMsgNotification,
    FloodPremiumWait,
    FloodWait,
    InternalServerError,
    RPCError,
    SecurityCheckMismatch,
    ServiceUnavailable,
    Unauthorized,
)
from pyrogram.raw.all import layer
from pyrogram.raw.core import FutureSalt, FutureSalts, Int, MsgContainer, TLObject

from .internals import MsgFactory

if TYPE_CHECKING:
    from collections.abc import Coroutine, Iterable

    from pyrogram.connection import Connection

log = logging.getLogger(__name__)


class SessionNotReady(TimeoutError):
    """This invocation never entered `send`; not a no-effect guarantee for its caller.

    Earlier invocations or preparation in a high-level method may already have effects.
    """

    def __init__(self, session: Session, query_name: str):
        self.session = session
        self.query_name = query_name
        self.no_send = True
        super().__init__(
            f'Waited {session.WAIT_TIMEOUT}s to invoke "{query_name}", and {session} is not started'
        )


@dataclass
class AlbumPublicationPolicy:
    """One caller task, client and primary session, for `messages.SendMultiMedia` only."""

    task: asyncio.Task | None
    client: pyrogram.Client
    session: Session | None
    attempted: bool = False
    succeeded: bool = False


_album_publication_policy: ContextVar[AlbumPublicationPolicy | None] = ContextVar(
    "_album_publication_policy", default=None
)


@contextmanager
def single_attempt_album_publication(client: pyrogram.Client):
    """Opt into one native publication attempt without changing shared client defaults.

    Child tasks inherit context but cannot use this policy. `attempted` fences off
    high-level parsing/enrichment errors after publication from readiness recovery.
    """
    policy = AlbumPublicationPolicy(asyncio.current_task(), client, client.session)
    token = _album_publication_policy.set(policy)
    try:
        yield policy
    finally:
        _album_publication_policy.reset(token)


_cleanup_origin: ContextVar[tuple[asyncio.Task | None, asyncio.Task | None] | None] = ContextVar(
    "_cleanup_origin", default=None
)


def _cleanup_caller() -> asyncio.Task | None:
    current = asyncio.current_task()
    inherited = _cleanup_origin.get()
    # Detached tasks inherit context, but are not synchronously awaiting this
    # cleanup. Only the helper task itself may stand in for its waiting caller.
    return inherited[1] if inherited is not None and inherited[0] is current else current


async def _finish_cleanup(coroutine: Coroutine[Any, Any, Any] | asyncio.Task) -> None:
    # Keep a strong reference and drain even if the owner is cancelled repeatedly.
    # Only cleanup runs separately; startup stays in its original caller task.
    if isinstance(coroutine, asyncio.Task):
        task = coroutine
    else:
        origin = _cleanup_caller()

        async def cleanup():
            token = _cleanup_origin.set((asyncio.current_task(), origin))
            try:
                return await coroutine
            finally:
                _cleanup_origin.reset(token)

        task = asyncio.create_task(cleanup())
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancelled is None:
                cancelled = error
        except Exception:
            break
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    task.result()


def _safe_error_detail(error: BaseException | None) -> str:
    # Arbitrary exception text can contain TL data, auth bytes or credential URLs.
    # Allow only details derived from known system codes, not the supplied text.
    if error is None:
        return "none"
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timed out"
    cause = error.__cause__ if error.__cause__ is not None else error
    if isinstance(cause, OSError) and isinstance(cause.errno, int) and 0 < cause.errno < 256:
        return os.strerror(cause.errno)[:120]
    return "omitted"


def _log_lifecycle(
    logger: logging.Logger,
    client: pyrogram.Client,
    dc_id: int,
    is_media: bool,
    is_cdn: bool,
    state: str,
    phase: str,
    attempt: int,
    error: BaseException | None = None,
    restart_active: bool = False,
    retry_delay: float | None = None,
    level: int | None = None,
    trigger: str = "none",
    trigger_code: int | None = None,
) -> None:
    # Names can contain phone numbers. Use a stable label, never the raw name.
    label = sha256(str(getattr(client, "name", "")).encode()).hexdigest()[:12]
    logger.log(
        level if level is not None else (logging.ERROR if error is not None else logging.INFO),
        "Session lifecycle client=%s dc=%s media=%s cdn=%s state=%s phase=%s "
        "attempt=%s restart_active=%s error=%s detail=%s retry_delay=%s "
        "trigger=%s trigger_code=%s",
        label,
        dc_id,
        is_media,
        is_cdn,
        state,
        phase,
        attempt,
        restart_active,
        type(error).__name__[:64] if error is not None else "none",
        _safe_error_detail(error),
        retry_delay,
        trigger,
        trigger_code,
    )


class SessionState(Enum):
    STARTING = auto()
    STARTED = auto()
    STOPPING = auto()
    STOPPED = auto()
    STOP_FAILED = auto()


class SessionCleanupError(RuntimeError):
    def __init__(self, failures: list[tuple[str, BaseException]]):
        self.failures = tuple(failures)
        super().__init__("Session cleanup failed; see sanitized lifecycle diagnostics")


class TransportError(Exception):
    pass


class AuthKeyNotFound(TransportError):
    pass


class TransportFlood(TransportError):
    pass


class InvalidDC(TransportError):
    pass


class Result:
    __slots__ = ("value", "event", "exception")

    def __init__(self):
        self.value: Any = None
        self.event: asyncio.Event = asyncio.Event()

        # Set instead of `value` when no answer can arrive; `send()` re-raises it.
        self.exception: Exception | None = None


class Session:
    START_TIMEOUT = 2
    STOP_TIMEOUT = 2
    WAIT_TIMEOUT = 15
    SLEEP_THRESHOLD = 10
    MAX_RETRIES = 10
    ACKS_THRESHOLD = 10
    PING_INTERVAL = 5
    RETRY_DELAY = 1
    STORED_MSG_IDS_MAX_SIZE = 1000 * 2
    CRYPTO_EXECUTOR_WORKERS = 1
    MAX_CONSECUTIVE_IGNORED = 30
    RESTART_RETRY_DELAY = 1
    RESTART_RETRY_MAX_DELAY = 30

    # TDLib asks for a new pool when the pool is empty or the current salt has under a
    #  minute left, and never more often than once a minute. Both thresholds and the
    #  count are TDLib's:
    #  https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/mtproto/AuthData.h#L233-L245
    #  https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/mtproto/SessionConnection.cpp#L949-L954
    FUTURE_SALTS_COUNT = 64
    FUTURE_SALTS_THRESHOLD = 60
    FUTURE_SALTS_INTERVAL = 60

    def __init__(
        self,
        client: pyrogram.Client,
        dc_id: int,
        server_address: str,
        port: int,
        auth_key: bytes,
        test_mode: bool,
        is_media: bool = False,
        is_cdn: bool = False,
        fallback_endpoints: Iterable[tuple[str, int]] | None = None,
    ):
        self.client = client
        self.dc_id = dc_id
        self.server_address = server_address
        self.port = port
        self.auth_key = auth_key
        self.test_mode = test_mode
        self.is_media = is_media
        self.is_cdn = is_cdn
        self.fallback_endpoints = tuple(
            dict.fromkeys(((server_address, port),) + tuple(fallback_endpoints or ()))
        )

        self.connection: Connection | None = None

        self._state = SessionState.STOPPED
        self._state_lock = asyncio.Lock()
        self._startup_phase = "idle"
        self._startup_attempt = 0
        self._stop_task: asyncio.Task | None = None
        self._initial_start_owner: asyncio.Task | None = None

        self.auth_key_id = sha1(auth_key).digest()[-8:]

        self.session_id = os.urandom(8)
        self.msg_factory = MsgFactory(self.client)

        self.salt = 0
        self.salt_valid_until: float = 0.0

        # A salt changes every 30 minutes and the old one is accepted for a further 1800
        #  seconds, so a session holding a single one is wrong within the hour:
        #  https://core.telegram.org/mtproto/description (Terminology, "Server Salt").
        self.future_salts: list[FutureSalt] = []
        self._future_salts_requested_at: float = 0.0

        self.ignore_count = 0

        self.pending_acks: set[int] = set()

        self.results: dict[int, Result] = {}

        self.stored_msg_ids: list[int] = []
        self.recent_msg_ids: list[int] = []

        self.ping_task: asyncio.Task | None = None
        self.ping_task_event = asyncio.Event()

        self.recv_task: asyncio.Task | None = None

        self.pending_tasks: set[asyncio.Task] = set()

        self.is_started = asyncio.Event()
        self.restart_lock = asyncio.Lock()
        self.restart_task: asyncio.Task | None = None

        # Never cleared: a stopped session is replaced rather than started again, since
        #  every caller that stops one then asks `Client.get_session()` to build a fresh one.
        self._must_stay_stopped: bool = False

    @property
    def state(self) -> SessionState:
        """Get current session state"""
        return self._state

    async def _set_state(self, new_state: SessionState) -> None:
        """Set session state"""
        async with self._state_lock:
            old_state = self._state
            self._state = new_state

            log.debug("Session state changed: %s -> %s", old_state.name, new_state.name)

    def _create_tracked_task(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task:
        # The set is what `stop()` waits on, and it is also the strong reference the
        #  loop does not hold: an unreferenced task can be collected mid-execution.
        #  https://docs.python.org/3/library/asyncio-task.html#creating-tasks
        task = asyncio.create_task(coroutine)

        self.pending_tasks.add(task)
        task.add_done_callback(self.pending_tasks.discard)

        return task

    async def _wait_pending_tasks(self, exclude: asyncio.Task | None = None) -> None:
        # A tracked `handle_packet` spawns a tracked `handle_updates`, so drain
        # successive generations. The worker requesting stop cannot join itself.
        while round_tasks := self.pending_tasks - {exclude}:
            round_tasks = set(round_tasks)
            _, running = await asyncio.wait(round_tasks, timeout=self.STOP_TIMEOUT)
            for task in running:
                task.cancel()
            for result in await asyncio.gather(*round_tasks, return_exceptions=True):
                if isinstance(result, Exception):
                    # Finished worker faults are diagnostic, not teardown failures.
                    self._log_lifecycle("cleanup-pending", self._startup_attempt, result)

    def _log_lifecycle(
        self,
        phase: str,
        attempt: int,
        error: BaseException | None = None,
        restart_active: bool | None = None,
        retry_delay: float | None = None,
        level: int | None = None,
        trigger: str = "none",
        trigger_code: int | None = None,
    ) -> None:
        _log_lifecycle(
            log,
            self.client,
            self.dc_id,
            self.is_media,
            self.is_cdn,
            self.state.name,
            phase,
            attempt,
            error,
            bool(self.restart_task and not self.restart_task.done())
            if restart_active is None
            else restart_active,
            retry_delay,
            level,
            trigger,
            trigger_code,
        )

    async def start(self):
        try:
            await self._start()
        except BaseException as error:
            if asyncio.current_task() is self._initial_start_owner:
                self._must_stay_stopped = True
            # Background retry owns its failure record, avoiding duplicate errors.
            if asyncio.current_task() is not self.restart_task:
                self._log_lifecycle(self._startup_phase, self._startup_attempt, error)
            try:
                await self._stop()
            except (Exception, asyncio.CancelledError):
                # Teardown reports its failures independently; preserve the origin.
                pass
            raise

    async def _start(self):
        if self._state in (SessionState.STARTED, SessionState.STARTING):
            log.debug("Session already started")
            return

        if self._must_stay_stopped:
            log.debug("Session must remain stopped")
            return

        for endpoint_index, (server_address, port) in enumerate(self.fallback_endpoints):
            if self._must_stay_stopped:
                return

            await self._set_state(SessionState.STARTING)
            self._startup_phase = "connect"
            self._startup_attempt = endpoint_index + 1
            self._log_lifecycle(
                self._startup_phase,
                self._startup_attempt,
                restart_active=bool(self.restart_task and (not self.restart_task.done())),
            )
            self.server_address = server_address
            self.port = port
            connection = self.client.connection_factory(
                dc_id=self.dc_id,
                server_address=server_address,
                port=port,
                test_mode=self.test_mode,
                proxy=self.client.proxy,
                media=self.is_media,
                protocol_factory=self.client.protocol_factory,
                crypto_executor_workers=self.CRYPTO_EXECUTOR_WORKERS,
            )
            self.connection = connection
            started_at = time.monotonic()

            try:
                await connection.connect()

                # An owner can stop the session while the transport is blocked
                # in connect(). Do not begin the MTProto handshake after that
                # stop has closed this connection.
                if self._must_stay_stopped:
                    # The owner's stop saw STARTING and closed this connection before
                    # its blocked connect() completed. It may have opened afterwards,
                    # while the session is already STOPPED, so close this exact transport.
                    await connection.close()
                    return

                self.recv_task = asyncio.create_task(self.recv_worker())
                self._startup_phase = "handshake"

                if self._must_stay_stopped:
                    await self._stop()
                    return

                await self.send(raw.functions.Ping(ping_id=0), timeout=self.START_TIMEOUT)

                init_connection_params = self.client.init_connection_params
                proxy_address = client_proxy_address(self.client.proxy)
                client_proxy: raw.types.InputClientProxy | None = None

                if proxy_address is not None:
                    client_proxy = raw.types.InputClientProxy(
                        address=proxy_address.hostname, port=proxy_address.port
                    )

                if isinstance(init_connection_params, dict):
                    init_connection_params = utils.obj_to_jsonvalue(init_connection_params)

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
                                params=init_connection_params,
                                proxy=client_proxy,
                            ),
                        ),
                        timeout=self.START_TIMEOUT,
                    )

                self.ping_task = asyncio.create_task(self.ping_worker())
            except (AuthKeyDuplicated, Unauthorized):
                raise
            except (OSError, RPCError, ConnectionError, TimeoutError) as e:
                elapsed = time.monotonic() - started_at
                try:
                    await self._stop()
                except SessionCleanupError:
                    raise e from None

                if self._must_stay_stopped:
                    return

                next_endpoint = (
                    self.fallback_endpoints[endpoint_index + 1]
                    if endpoint_index + 1 < len(self.fallback_endpoints)
                    else None
                )

                if next_endpoint is None:
                    raise ConnectionError(
                        f"Unable to start session on DC{self.dc_id} via "
                        f"{len(self.fallback_endpoints)} endpoint(s)"
                    ) from e

                log.warning(
                    "DC%s endpoint %s:%s failed after %.3fs with %s: %s; falling back to %s:%s",
                    self.dc_id,
                    server_address,
                    port,
                    elapsed,
                    e.__class__.__name__,
                    _safe_error_detail(e),
                    next_endpoint[0],
                    next_endpoint[1],
                )
                continue
            else:
                self.fallback_endpoints = tuple(
                    dict.fromkeys(((server_address, port),) + self.fallback_endpoints)
                )
                break

        if self._must_stay_stopped:
            await self._stop()
            return

        log.info("Session initialized: Pyrogram v%s (Layer %s)", pyrogram.__version__, layer)
        log.info("Device: %s - %s", self.client.device_model, self.client.app_version)
        log.info("System: %s (%s)", self.client.system_version, self.client.lang_code)

        await self._set_state(SessionState.STARTED)
        self.is_started.set()

        log.info("Session started")

        if callable(self.client.connect_handler):
            self._startup_phase = "connect-callback"
            try:
                await self.client.connect_handler(self.client, self)
            except Exception as e:
                self._log_lifecycle("connect-callback", self._startup_attempt, e)
        self._startup_phase = "ready"

    async def stop(self):
        # `restart()` reads this, and the flag is set before the state check below so a
        #  stop that arrives while a restart is already between its own `stop()` and
        #  `start()` still counts.
        self._must_stay_stopped = True

        await self._stop()

    def _has_live_workers(self) -> bool:
        workers = self.pending_tasks | {
            self.ping_task,
            self.recv_task,
            self.restart_task,
            self._initial_start_owner,
        }
        return any(task is not None and not task.done() for task in workers)

    async def _stop(self) -> None:
        current = _cleanup_caller()
        task = self._stop_task
        if task is not None and not task.done():
            # A worker being drained cannot join its own owner. Its stop request
            # is honored, but only independent callers can wait for completion.
            if current in self.pending_tasks or current in (self.recv_task, self.ping_task):
                return
            await _finish_cleanup(task)
            return
        if self._state is SessionState.STOP_FAILED and task is not None:
            task.result()
        if self._state is SessionState.STOPPED:
            log.debug("Session already stopped")
            return

        # Publish the real owner before yielding, not another wrapper around stop.
        task = self._stop_task = asyncio.create_task(self._stop_resources(current))
        cleanup_error = None
        try:
            await _finish_cleanup(task)
        except SessionCleanupError as error:
            cleanup_error = error

        # Resources settle before callbacks, so cross-task callback reentry cannot
        # wait for the callback which is awaiting it.
        if callable(self.client.disconnect_handler):
            try:
                await self.client.disconnect_handler(self.client, self)
            except Exception as e:
                self._log_lifecycle("disconnect-callback", self._startup_attempt, e)
        if cleanup_error is not None:
            raise cleanup_error

    async def _stop_resources(self, origin: asyncio.Task | None) -> None:
        await self._set_state(SessionState.STOPPING)

        self.ignore_count = 0

        self.is_started.clear()

        self.stored_msg_ids.clear()

        # The unsent acks name msg ids of the connection this stop closes, which the
        #  server cannot match after a reconnect.
        self.pending_acks.clear()

        # A pending waiter's msg id also dies with the connection and nothing re-sends
        #  the request, so no answer can arrive: failing each waiter here turns a
        #  silent `WAIT_TIMEOUT` into an immediate, accurate error.
        for result in self.results.values():
            result.exception = TimeoutError("Session stopped before an answer arrived")
            result.event.set()

        self.results.clear()

        self.ping_task_event.set()

        failures: list[tuple[str, BaseException]] = []

        async def drain(phase, awaitable, worker=None):
            try:
                await awaitable
                return True
            except (Exception, asyncio.CancelledError) as error:
                self._log_lifecycle(f"cleanup-{phase}", self._startup_attempt, error)
                # Only an exception from the finished worker is diagnostic. Keep
                # cancellation, drain timeouts and provider close errors explicit.
                if (
                    worker is not None
                    and worker.done()
                    and not worker.cancelled()
                    and worker.exception() is error
                ):
                    return True
                failures.append((phase, error))
                return False

        if self.ping_task is not None and self.ping_task is not origin:
            # A send already in flight uses the transport's budget, not the
            # update-worker grace period. Do not cancel a healthy provider send.
            await drain("ping", self.ping_task, self.ping_task)
            self.ping_task = None
        # A ping worker requesting stop must still see the signal when its
        # synchronous caller resumes; it cannot be joined by its own cleanup.
        if self.ping_task is not origin:
            self.ping_task_event.clear()

        # Providers own their close deadlines (WEB DELETE alone can take 20s).
        # The shielded stop owner must let them settle before releasing workers.
        closed = self.connection is None or await drain("close", self.connection.close())
        if not closed:
            # An injected provider may reject close. Do not claim the transport
            # is closed or silently reconnect this object over it.
            self._must_stay_stopped = True

        if self.recv_task is not None and self.recv_task is not origin:
            if not closed and not self.recv_task.done():
                self.recv_task.cancel()
            await drain("recv", asyncio.wait_for(self.recv_task, self.STOP_TIMEOUT), self.recv_task)
            self.recv_task = None

        await drain("pending", self._wait_pending_tasks(origin))
        await self._set_state(SessionState.STOPPED if closed else SessionState.STOP_FAILED)
        if failures:
            raise SessionCleanupError(failures)
        log.info("Session stopped")

    async def restart(self):
        async with self.restart_lock:
            if self.stored_msg_ids:
                self.recent_msg_ids = self.stored_msg_ids[:30]

            await self._stop()

            if self._must_stay_stopped:
                return

            await self.start()

            # `stop()` does not acquire `restart_lock`: it may race the call
            # above and must win even if `start()` completed its handshake.
            if self._must_stay_stopped:
                await self._stop()

    def schedule_restart(self, reason: str):
        if self.restart_task and not self.restart_task.done():
            log.debug("Session restart already scheduled")
            return

        self.restart_task = asyncio.create_task(self._restart_until_started(reason))

    async def _restart_until_started(self, reason: str):
        retry_delay = self.RESTART_RETRY_DELAY
        attempt = 0
        last_failure = None
        trigger = reason if reason in {"recv-null", "unpack", "validation", "ping"} else "external"
        trigger_code = None
        if reason.startswith("recv-transport:"):
            code = reason.removeprefix("recv-transport:")
            if code.isdecimal() and len(code) <= 4:
                trigger, trigger_code = "recv-transport", int(code)

        while getattr(self.client, "is_connected", True) and not self._must_stay_stopped:
            attempt += 1
            self._log_lifecycle(
                "reconnect",
                attempt,
                restart_active=True,
                trigger=trigger,
                trigger_code=trigger_code,
            )
            try:
                await self.restart()
            except (AuthKeyDuplicated, Unauthorized) as error:
                self._log_lifecycle(
                    "reconnect-aborted",
                    attempt,
                    error,
                    True,
                    trigger=trigger,
                    trigger_code=trigger_code,
                )
                return
            except Exception as e:
                cause = e.__cause__ if e.__cause__ is not None else e
                failure = (type(e), type(cause), self._startup_phase, _safe_error_detail(e))
                self._log_lifecycle(
                    "reconnect-backoff",
                    attempt,
                    e,
                    True,
                    retry_delay,
                    level=logging.ERROR if failure != last_failure else logging.WARNING,
                    trigger=trigger,
                    trigger_code=trigger_code,
                )
                last_failure = failure
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, self.RESTART_RETRY_MAX_DELAY)
            else:
                self._log_lifecycle("reconnect-ready", attempt, restart_active=True)
                return

        self._log_lifecycle("reconnect-stopped", attempt, restart_active=True)

    async def handle_packet(self, packet):
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                self.connection.protocol.crypto_executor,
                mtproto.unpack,
                BytesIO(packet),
                self.session_id,
                self.auth_key,
                self.auth_key_id,
            )
        except ValueError as e:
            log.debug(e)
            log.info("Restarting session due to - %s - %s", e.__class__.__name__, e)
            self.schedule_restart("unpack")
            return

        messages = data.body.messages if isinstance(data.body, MsgContainer) else [data]

        log.debug("Received: %s", data)

        for msg in messages:
            if msg.msg_id in self.recent_msg_ids or msg.msg_id in self.stored_msg_ids:
                # A known retransmission still needs an ACK, but must not replay its
                # body, change the clock, or count towards a security restart.
                # https://core.telegram.org/mtproto/service_messages#message-copies
                if msg.seq_no % 2 != 0:
                    self.pending_acks.add(msg.msg_id)
                continue

            # Preserve initial clock synchronization and recovery from bad client time
            # before applying the time window. Known duplicates cannot reach this path.
            if msg.seq_no == 0:
                self.client._set_server_time(msg.msg_id)

            try:
                if len(self.stored_msg_ids) > Session.STORED_MSG_IDS_MAX_SIZE:
                    del self.stored_msg_ids[: Session.STORED_MSG_IDS_MAX_SIZE // 2]

                if self.stored_msg_ids:
                    if msg.msg_id < self.stored_msg_ids[0]:
                        raise SecurityCheckMismatch(
                            "The msg_id is lower than all the stored values"
                        )

                    time_diff = (
                        msg.msg_id - (await self.msg_factory.allocate_message_identity())
                    ) / 2**32

                    if time_diff > 30:
                        raise SecurityCheckMismatch(
                            "The msg_id belongs to over 30 seconds in the future. "
                            "Most likely the client time has to be synchronized."
                        )

                    if time_diff < -300:
                        raise SecurityCheckMismatch(
                            "The msg_id belongs to over 300 seconds in the past. "
                            "Most likely the client time has to be synchronized."
                        )

            except SecurityCheckMismatch as e:
                log.info("Discarding message: %s", e)

                self.ignore_count += 1

                if self.ignore_count >= self.MAX_CONSECUTIVE_IGNORED:
                    log.info("Restarting session due to - %s - %s", e.__class__.__name__, e)
                    self.schedule_restart("validation")
                    return

                # A container can mix invalid messages with a fresh handshake reply.
                # Reject this item without dropping independently validated siblings.
                continue
            else:
                bisect.insort(self.stored_msg_ids, msg.msg_id)
                self.ignore_count = 0

            if msg.seq_no % 2 != 0:
                # `MsgDetailedInfo` can queue an ACK before the answer body arrives,
                # so membership in `pending_acks` alone cannot identify a duplicate.
                self.pending_acks.add(msg.msg_id)

            if isinstance(msg.body, (raw.types.MsgDetailedInfo, raw.types.MsgNewDetailedInfo)):
                self.pending_acks.add(msg.body.answer_msg_id)
                continue

            if isinstance(msg.body, raw.types.NewSessionCreated):
                continue

            msg_id = None

            if isinstance(msg.body, raw.types.BadServerSalt):
                msg_id = msg.body.bad_msg_id

                # Taken here rather than in `send()`, which only ever sees an answer
                #  somebody awaited. `ping_worker` sends with `wait_response=False`, so a
                #  salt offered in reply to a ping was dropped and an idle media session
                #  kept a retired one until every request on it timed out.
                #  https://core.telegram.org/mtproto/service_messages_about_messages#notice-of-ignored-error-message
                self.salt = msg.body.new_server_salt
            elif isinstance(msg.body, raw.types.BadMsgNotification):
                msg_id = msg.body.bad_msg_id
            elif isinstance(msg.body, (FutureSalts, raw.types.RpcResult)):
                msg_id = msg.body.req_msg_id
            elif isinstance(msg.body, raw.types.Pong):
                msg_id = msg.body.msg_id
            else:
                if self.client is not None:
                    self._create_tracked_task(self.client.handle_updates(msg.body))

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

    def _current_salt(self, server_time: float) -> int:
        """Get the salt valid at `server_time`, dropping the ones it has passed"""
        while self.future_salts and self.future_salts[0].valid_since <= server_time:
            salt = self.future_salts.pop(0)

            self.salt = salt.salt
            self.salt_valid_until = salt.valid_until

        return self.salt

    async def _update_future_salts(self) -> None:
        server_time = self.client.server_time

        if server_time - self._future_salts_requested_at < self.FUTURE_SALTS_INTERVAL:
            return

        # Promote first, so `salt_valid_until` is the one actually in use right now.
        self._current_salt(server_time)

        if self.future_salts and self.salt_valid_until - server_time > self.FUTURE_SALTS_THRESHOLD:
            return

        self._future_salts_requested_at = server_time

        # The same budget `start()` gives its own round trips, rather than the whole
        #  `WAIT_TIMEOUT`: `stop()` waits on this task, and the request is pre-emptive,
        #  so an answer that does not arrive is asked for again a minute later.
        future_salts = await self.send(
            raw.functions.GetFutureSalts(num=self.FUTURE_SALTS_COUNT),
            timeout=self.START_TIMEOUT,
        )

        self.future_salts = sorted(future_salts.salts, key=lambda salt: salt.valid_since)

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
                        ping_id=await self.msg_factory.allocate_message_identity(),
                        disconnect_delay=self.WAIT_TIMEOUT + 10,
                    ),
                    wait_response=False,
                )
            except OSError as e:
                log.info("Restarting session due to - %s - %s", e.__class__.__name__, e)
                self.schedule_restart("ping")
                break
            except RPCError:
                pass

            try:
                await self._update_future_salts()

            # Only logged: the current salt still has a minute of life, and a
            #  `BadServerSalt` recovers the session anyway. `send` reports an answer
            #  that never came as `TimeoutError`, which is an `OSError`.
            except (OSError, RPCError) as e:
                log.info("Could not get future salts - %s - %s", e.__class__.__name__, e)

        log.info("PingTask stopped")

    async def recv_worker(self):
        log.info("NetworkTask started")

        while True:
            packet = await self.connection.recv()

            if packet is None or len(packet) == 4:
                if packet:
                    error_code = -Int.read(BytesIO(packet))
                    error_msg = "unknown error"

                    if error_code == 404:
                        raise AuthKeyNotFound(
                            "Auth key not found in the system. Try again or delete your session file "
                            "and log in again with your phone number or bot token."
                        )

                    try:
                        if error_code == 429:
                            raise TransportFlood("Transport flood. Please slow down your requests.")
                        elif error_code == 444:
                            raise InvalidDC("Invalid data center. Please check your configuration.")
                    except TransportError as e:
                        error_msg = str(e)

                    log.warning("Server sent transport error: %s (%s)", error_code, error_msg)

                if self.is_started.is_set():
                    if packet:
                        error = f"Server sent transport error - {error_code} - ({error_msg})."
                    else:
                        error = "Server sent a null packet."

                    log.info("Restarting session due to - %s", error)
                    self.schedule_restart(f"recv-transport:{error_code}" if packet else "recv-null")

                break

            self._create_tracked_task(self.handle_packet(packet))

        log.info("NetworkTask stopped")

    async def send(self, data: TLObject, wait_response: bool = True, timeout: float = WAIT_TIMEOUT):
        message = await self.msg_factory.create(data)
        msg_id = message.msg_id

        # Held locally as well: `_stop()` empties `self.results` when it fails the
        #  pending waiters, so the dict entry may be gone by the time the wait ends.
        pending_result: Result | None = None

        if wait_response:
            pending_result = Result()
            self.results[msg_id] = pending_result

        log.debug("Sent: %s", message)

        payload = await asyncio.get_running_loop().run_in_executor(
            self.connection.protocol.crypto_executor,
            mtproto.pack,
            message,
            self._current_salt(self.client.server_time),
            self.session_id,
            self.auth_key,
            self.auth_key_id,
        )

        try:
            await self.connection.send(payload)
        except OSError as e:
            self.results.pop(msg_id, None)
            raise e

        if pending_result is not None:
            try:
                await asyncio.wait_for(pending_result.event.wait(), timeout)
            except asyncio.TimeoutError:
                pass

            self.results.pop(msg_id, None)

            if pending_result.exception is not None:
                raise pending_result.exception

            result = pending_result.value

            if result is None:
                raise TimeoutError("Request timed out")

            if isinstance(result, raw.types.RpcError):
                if isinstance(
                    data, (raw.functions.InvokeWithoutUpdates, raw.functions.InvokeWithTakeout)
                ):
                    data = data.query

                RPCError.raise_it(result, type(data))

            if isinstance(result, raw.types.BadMsgNotification):
                log.warning(
                    "%s: %s", BadMsgNotification.__name__, BadMsgNotification(result.error_code)
                )

            # `handle_packet` has already taken the new salt, so this only re-sends.
            if isinstance(result, raw.types.BadServerSalt):
                return await self.send(data, wait_response, timeout)

            return result

    async def invoke(
        self,
        query: TLObject,
        retries: int = MAX_RETRIES,
        timeout: float = WAIT_TIMEOUT,
        sleep_threshold: float = SLEEP_THRESHOLD,
        retry_delay: float = RETRY_DELAY,
    ):
        inner_query = query
        while isinstance(
            inner_query, (raw.functions.InvokeWithoutUpdates, raw.functions.InvokeWithTakeout)
        ):
            inner_query = inner_query.query

        query_name = ".".join(inner_query.QUALNAME.split(".")[1:])

        policy = _album_publication_policy.get()
        scoped_publication = (
            policy is not None
            and policy.task is asyncio.current_task()
            and policy.client is self.client
            and isinstance(inner_query, raw.functions.messages.SendMultiMedia)
        )
        if scoped_publication and policy is not None:
            if policy.session is not self:
                # A same-owner publication may not inherit native retries when
                # peer resolution changed the primary session after scope entry.
                raise RuntimeError("album_publication_session_changed")
            retries = 1

        try:
            await asyncio.wait_for(self.is_started.wait(), self.WAIT_TIMEOUT)

        # Carrying on instead reaches `send()`, which reads `self.connection.protocol` on a
        #  session whose `connection` is still `None`: `AttributeError: 'NoneType' object has
        #  no attribute 'protocol'`, naming neither the session nor the query.
        except asyncio.TimeoutError as e:
            raise SessionNotReady(self, query_name) from e

        for attempt in range(1, retries + 1):
            try:
                if scoped_publication and policy is not None:
                    policy.attempted = True
                result = await self.send(query, timeout=timeout)
                if scoped_publication and policy is not None:
                    policy.succeeded = True
                return result
            except (FloodWait, FloodPremiumWait) as e:
                amount = e.seconds

                if scoped_publication or amount is None or amount > sleep_threshold >= 0:
                    raise

                log.warning(
                    '[%s] Waiting for %s seconds before continuing (required by "%s")',
                    self.client.name,
                    amount,
                    query_name,
                )

                await asyncio.sleep(amount)
            except (OSError, InternalServerError, ServiceUnavailable) as e:
                if scoped_publication:
                    raise
                # `TCP.send` raises a bare `TimeoutError`, an `OSError` whose `str()` is
                #  empty, so without the `repr` fallback the line would end at "due to: ".
                #  `pyrogram/connection/transport/tcp/tcp.py:505`.
                log.warning(
                    '[%s] Retrying "%s" (attempt %s) due to: %s',
                    self.client.name,
                    query_name,
                    attempt,
                    str(e) or repr(e),
                )

                await asyncio.sleep(retry_delay)

        raise TimeoutError(f'Failed to invoke "{query_name}" after {retries} retries')

    def __str__(self) -> str:
        return f"Session(dc_id={self.dc_id}, test_mode={self.test_mode}, is_media={self.is_media}, is_cdn={self.is_cdn}, state={self._state.name})"

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

"""SDK lifecycle and encrypted MTProto over an offline transport.

Successful off-DC auth-key creation is stubbed in authorization/isolation tests;
its cancellation test exercises the real `Auth.create`. Session methods are real.
"""

from __future__ import annotations as _annotations

import asyncio
import errno
import hashlib
import inspect
import logging
import os
import socket
import sqlite3
from io import BytesIO, StringIO
from types import SimpleNamespace
from typing import Final

import pytest

import pyrogram
from pyrogram import raw
from pyrogram.crypto import aes, mtproto
from pyrogram.errors import AuthBytesInvalid
from pyrogram.file_id import FileId, FileType
from pyrogram.raw.core import FutureSalt, FutureSalts, Long, Message
from pyrogram.session.session import Session, SessionCleanupError, SessionState

FIXTURE: Final = b"offline fixture media"
AUTH_KEY: Final = bytes(256)
DC: Final = 5


def config():
    kwargs = {}
    for name, p in inspect.signature(raw.types.Config).parameters.items():
        if p.default is not inspect.Parameter.empty:
            continue
        kwargs[name] = [] if name == "dc_options" else ("" if p.annotation == "str" else 0)
    kwargs.update(test_mode=True, this_dc=DC, webfile_dc_id=DC)
    return raw.types.Config(**kwargs)


class Storage:
    async def dc_id(self):
        return DC

    async def test_mode(self):
        return True

    async def api_id(self):
        return 1

    async def auth_key(self):
        return AUTH_KEY

    async def update_peers(self, peers):
        pass


class Transport:
    def __init__(self, owner, media, mode, dc_id):
        self.owner = owner
        self.media = media
        self.mode = mode
        self.dc_id = dc_id
        self.protocol = SimpleNamespace(crypto_executor=None)
        self.incoming = asyncio.Queue()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.block_close = False
        self.closed = False
        self.close_calls = 0
        self.requests = []
        self.import_entered = asyncio.Event()
        self.import_release = asyncio.Event()
        self.request_entered = asyncio.Event()
        self.request_release = asyncio.Event()
        self.seq = 0

    async def connect(self):
        self.entered.set()
        if self.mode == "block":
            await self.release.wait()
        elif self.mode == "error":
            raise OSError("fixture connect failure")
        elif self.mode == "secret_error":
            raise OSError(
                errno.ECONNREFUSED, "https://user:password@example.test/?token=secret +84901234567"
            )

    async def close(self):
        self.close_entered.set()
        if self.block_close:
            await self.close_release.wait()
        self.closed = True
        self.close_calls += 1
        await self.incoming.put(None)

    async def recv(self):
        return await self.incoming.get()

    async def send(self, payload):
        assert not self.closed, "SDK sent on a closed fixture connection"
        key, iv = mtproto.kdf(AUTH_KEY, payload[8:24], True)
        plain = BytesIO(aes.ige256_decrypt(payload[24:], key, iv))
        plain.read(8)
        session_id = plain.read(8)
        request = Message.read(plain)
        body = request.body
        self.requests.append(body.QUALNAME)
        if isinstance(body, raw.types.MsgsAck):
            return
        if isinstance(body, (raw.functions.Ping, raw.functions.PingDelayDisconnect)):
            if self.mode == "handshake_block":
                self.request_entered.set()
                await self.request_release.wait()
            response = raw.types.Pong(msg_id=request.msg_id, ping_id=body.ping_id)
        elif isinstance(body, (raw.functions.InvokeWithLayer, raw.functions.help.GetConfig)):
            result = (
                raw.types.RpcError(error_code=401, error_message="AUTH_KEY_UNREGISTERED")
                if self.mode == "auth_error"
                else config()
            )
            response = raw.types.RpcResult(req_msg_id=request.msg_id, result=result)
        elif isinstance(body, raw.functions.GetFutureSalts):
            now = int(self.owner.server_time)
            response = FutureSalts(
                req_msg_id=request.msg_id,
                now=now,
                salts=[
                    FutureSalt(valid_since=now - 1, valid_until=now + 1800, salt=1),
                    FutureSalt(valid_since=now + 1800, valid_until=now + 3600, salt=2),
                ],
            )
        elif isinstance(body, raw.functions.auth.ExportAuthorization):
            self.request_entered.set()
            if self.mode == "export_block":
                await self.request_release.wait()
            if self.mode == "export_error":
                response = raw.types.RpcResult(
                    req_msg_id=request.msg_id,
                    result=raw.types.RpcError(error_code=400, error_message="AUTH_BYTES_INVALID"),
                )
                await self.reply(response, session_id)
                return
            response = raw.types.RpcResult(
                req_msg_id=request.msg_id,
                result=raw.types.auth.ExportedAuthorization(id=1, bytes=b"fixture"),
            )
        elif isinstance(body, raw.functions.auth.ImportAuthorization):
            self.import_entered.set()
            if self.mode == "import_block":
                await self.import_release.wait()
            if self.mode == "import_error":
                result = raw.types.RpcError(error_code=400, error_message="AUTH_BYTES_INVALID")
            else:
                result = raw.types.auth.Authorization(user=raw.types.User(id=1))
            response = raw.types.RpcResult(req_msg_id=request.msg_id, result=result)
        elif isinstance(body, raw.functions.upload.GetFile):
            response = raw.types.RpcResult(
                req_msg_id=request.msg_id,
                result=raw.types.upload.File(
                    type=raw.types.storage.FileUnknown(), mtime=0, bytes=FIXTURE
                ),
            )
        else:
            raise AssertionError(f"Unhandled fixture request: {body.QUALNAME}")
        await self.reply(response, session_id)

    async def reply(self, response, session_id):
        msg_id = await self.owner.session.msg_factory.allocate_message_identity() + 1
        self.seq += 1
        message = Message(response, msg_id, 2 * self.seq + 1, len(response))
        data = Long(0) + session_id + message.write()
        padding = os.urandom(-(len(data) + 12) % 16 + 12)
        msg_key = hashlib.sha256(AUTH_KEY[96:128] + data + padding).digest()[8:24]
        key, iv = mtproto.kdf(AUTH_KEY, msg_key, False)
        packet = (
            hashlib.sha1(AUTH_KEY).digest()[-8:]
            + msg_key
            + aes.ige256_encrypt(data + padding, key, iv)
        )
        await self.incoming.put(packet)


class Harness:
    def __init__(self):
        self.modes = {}
        self.transports = []
        self.owned = []
        self.client = pyrogram.Client(
            "offline-lifecycle", api_id=1, api_hash="0" * 32, in_memory=True
        )
        self.client.storage = Storage()
        self.client.is_connected = True
        self.client.connection_factory = self.factory
        self.client.get_dc_option = self.dc_option
        self.client.connect_handler = self.remember
        self.client.session = Session(self.client, DC, "127.0.0.1", 443, AUTH_KEY, True)
        self.owned.append(self.client.session)

    async def remember(self, client, session):
        self.owned.append(session)

    def factory(self, **kwargs):
        key = (kwargs["dc_id"], kwargs["media"])
        mode = self.modes.get(key, "ok")
        c = Transport(self.client, kwargs["media"], mode, kwargs["dc_id"])
        self.transports.append(c)
        return c

    async def dc_option(self, dc_id, **kwargs):
        return raw.types.DcOption(
            id=dc_id, ip_address="127.0.0.1", port=443, media_only=kwargs.get("is_media")
        )

    async def main_control(self):
        result = await self.client.session.invoke(raw.functions.help.GetConfig())
        assert isinstance(result, raw.types.Config)
        assert self.client.session.state is SessionState.STARTED

    async def media(self, dc=DC, **kwargs):
        return await self.client.get_session(dc, is_media=True, **kwargs)

    async def download(self):
        fid = FileId(file_type=FileType.DOCUMENT, dc_id=DC, media_id=1, access_hash=1)
        return b"".join([part async for part in self.client.get_file(fid)])


async def wait_until(predicate):
    async def spin():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), 2)


@pytest.fixture
async def h(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("offline lifecycle test tried to open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    harness = Harness()
    await asyncio.wait_for(harness.client.session.start(), 2)
    try:
        yield harness
    finally:
        harness.client.connect_handler = None
        harness.client.disconnect_handler = None
        harness.client.is_connected = False
        for transport in harness.transports:
            transport.release.set()
            transport.close_release.set()
            transport.import_release.set()
            transport.request_release.set()
        sessions = set(
            harness.owned
            + list(harness.client.media_sessions.values())
            + list(harness.client.sessions.values())
        )
        for session in sessions:
            if session.restart_task and not session.restart_task.done():
                session.restart_task.cancel()
                await asyncio.gather(session.restart_task, return_exceptions=True)
            # Observe failures in assertions first, then avoid replaying a failed
            # worker during old-source negative-control fixture cleanup.
            for name in ("recv_task", "ping_task"):
                worker = getattr(session, name)
                if worker is not None and worker.done():
                    await asyncio.gather(worker, return_exceptions=True)
                    setattr(session, name, None)
            # A factory failure in the baseline has no transport to release.
            if session.connection is None:
                continue
            # Old code can leave an interrupted stop, repair only in fixture teardown.
            if session.state.name in {"STOPPING", "STOP_FAILED"}:
                session._state = SessionState.STARTING
            await session.stop()
        harness.client.executor.shutdown(wait=True)


async def blocked_creator(h):
    h.modes[(DC, True)] = "block"
    creator = asyncio.create_task(h.media())
    await wait_until(lambda: DC in h.client.media_sessions and h.transports[-1].entered.is_set())
    session = h.client.media_sessions[DC]
    h.owned.append(session)
    return creator, session, h.transports[-1]


async def test_initial_connect_cancel_rolls_back_and_reacquires(h):
    creator, session, transport = await blocked_creator(h)
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert DC not in h.client.media_sessions
    assert session.state is SessionState.STOPPED
    assert transport.closed
    h.modes[(DC, True)] = "ok"
    replacement = await h.media()
    assert replacement is not session
    assert await h.download() == FIXTURE
    await h.main_control()


async def test_initial_connect_failure_rolls_back(h):
    h.modes[(DC, True)] = "error"
    with pytest.raises(ConnectionError):
        await h.media()
    assert DC not in h.client.media_sessions
    assert h.transports[-1].closed
    h.modes[(DC, True)] = "ok"
    assert (await h.media()).is_started.is_set()
    assert await h.download() == FIXTURE


async def test_borrower_cancel_does_not_cancel_initial_owner(h):
    creator, session, transport = await blocked_creator(h)
    # Cache publication is intentional: a borrower does not own startup/cleanup.
    assert await h.media() is session
    waiter = asyncio.create_task(session.invoke(raw.functions.help.GetConfig()))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not creator.done()
    assert not transport.closed
    transport.release.set()
    assert await creator is session
    assert await h.download() == FIXTURE


async def test_repeated_cancel_during_cleanup_still_closes_resource(h):
    creator, session, transport = await blocked_creator(h)
    transport.block_close = True
    creator.cancel()
    await asyncio.wait_for(transport.close_entered.wait(), 2)
    creator.cancel()
    await asyncio.sleep(0)
    creator.cancel()
    await asyncio.sleep(0)
    assert not creator.done()
    transport.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert transport.closed
    assert session.state is SessionState.STOPPED
    assert DC not in h.client.media_sessions


async def test_failed_old_creator_cannot_evict_new_generation(h):
    creator, old, old_transport = await blocked_creator(h)
    del h.client.media_sessions[DC]
    h.modes[(DC, True)] = "ok"
    new = await h.media()
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert h.client.media_sessions[DC] is new
    assert old_transport.closed
    assert old.state is SessionState.STOPPED
    assert new.is_started.is_set()
    assert await h.download() == FIXTURE


async def test_connect_callback_reentry_and_cancel_is_owned(h):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def callback(client, session):
        if not session.is_media:
            return
        assert await client.get_session(DC, is_media=True) is session
        assert await asyncio.create_task(client.get_session(DC, is_media=True)) is session
        entered.set()
        await release.wait()

    h.client.connect_handler = callback
    creator = asyncio.create_task(h.media())
    await asyncio.wait_for(entered.wait(), 2)
    session = h.client.media_sessions[DC]
    h.owned.append(session)
    transport = session.connection
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert DC not in h.client.media_sessions
    assert transport.closed
    assert session.state is SessionState.STOPPED


@pytest.mark.parametrize("temporary", [False, True])
@pytest.mark.parametrize("mode", ["import_error", "import_block"])
async def test_import_failure_or_cancel_closes_only_created_session(
    h, monkeypatch, temporary, mode
):
    class FixtureAuth:
        connection = None

        def __init__(self, *args, **kwargs):
            pass

        async def create(self):
            return AUTH_KEY

    monkeypatch.setattr("pyrogram.client.Auth", FixtureAuth)
    h.modes[(4, False)] = mode
    creator = asyncio.create_task(h.client.get_session(4, temporary=temporary))
    await wait_until(lambda: any(t.dc_id == 4 and t.import_entered.is_set() for t in h.transports))
    transport = next(t for t in h.transports if t.dc_id == 4)
    if not temporary:
        # Error may already have evicted it on the candidate.
        if 4 in h.client.sessions:
            h.owned.append(h.client.sessions[4])
    if mode == "import_block":
        creator.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creator
    else:
        with pytest.raises(AuthBytesInvalid):
            await creator
    assert 4 not in h.client.sessions
    assert transport.closed
    await h.main_control()


async def test_active_reconnect_and_backoff_keep_same_cache_owner(h):
    session = await h.media()
    h.modes[(DC, True)] = "block"
    await session.connection.incoming.put(None)
    await wait_until(
        lambda: (
            session.restart_task
            and h.transports[-1].mode == "block"
            and h.transports[-1].entered.is_set()
        )
    )
    assert session.state is SessionState.STARTING
    assert await h.media() is session
    assert session.restart_lock.locked()
    session.WAIT_TIMEOUT = 0.01
    with pytest.raises(TimeoutError):
        await h.download()
    await h.main_control()
    h.transports[-1].release.set()
    await asyncio.wait_for(session.restart_task, 2)
    assert await h.download() == FIXTURE
    h.modes[(DC, True)] = "error"
    await session.connection.incoming.put(None)
    await wait_until(
        lambda: (
            session.restart_task
            and not session.restart_task.done()
            and session.state is SessionState.STOPPED
        )
    )
    assert await h.media() is session
    h.modes[(DC, True)] = "ok"
    await asyncio.wait_for(session.restart_task, 2)
    assert await h.download() == FIXTURE


async def test_terminal_stop_and_cached_fast_path_are_unchanged(h):
    session = await h.media()
    transports = len(h.transports)
    # A healthy hit must complete without yielding to another task.
    marker = []
    asyncio.get_running_loop().call_soon(marker.append, True)
    assert await h.media() is session
    assert not marker
    assert len(h.transports) == transports
    await session.stop()
    await session.start()
    await session.restart()
    assert session.state is SessionState.STOPPED
    assert session._must_stay_stopped
    assert len(h.transports) == transports
    assert await h.media() is session


async def test_unrelated_media_dc_remains_ready_during_blocked_owner(h, monkeypatch):
    class FixtureAuth:
        connection = None

        def __init__(self, *args, **kwargs):
            pass

        async def create(self):
            return AUTH_KEY

    monkeypatch.setattr("pyrogram.client.Auth", FixtureAuth)
    creator, _, transport = await blocked_creator(h)
    independent = await asyncio.wait_for(h.media(4), 2)
    assert independent.is_started.is_set()
    assert (await h.client.get_session(4)).is_started.is_set()
    await h.main_control()
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert transport.closed
    assert independent.is_started.is_set()
    assert isinstance(await independent.invoke(raw.functions.help.GetConfig()), raw.types.Config)


async def test_prerequisite_race_reuses_published_generation(h):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    original = h.client.get_dc_option

    async def blocked_option(dc_id, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(dc_id, **kwargs)

    h.client.get_dc_option = blocked_option
    slow = asyncio.create_task(h.media())
    await asyncio.wait_for(entered.wait(), 2)
    winner = await h.media()
    count = len(h.transports)
    release.set()
    assert await slow is winner
    assert h.client.media_sessions[DC] is winner
    assert len(h.transports) == count
    assert await h.download() == FIXTURE


async def test_real_auth_prerequisite_repeated_cancel_closes_transport(h):
    h.modes[(4, False)] = "block"
    creator = asyncio.create_task(h.client.get_session(4))
    await wait_until(lambda: h.transports[-1].dc_id == 4 and h.transports[-1].entered.is_set())
    transport = h.transports[-1]
    transport.block_close = True
    creator.cancel()
    await asyncio.wait_for(transport.close_entered.wait(), 2)
    creator.cancel()
    await asyncio.sleep(0)
    creator.cancel()
    await asyncio.sleep(0)
    assert not creator.done()
    transport.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert transport.closed
    assert 4 not in h.client.sessions
    assert 4 not in h.client.media_sessions
    await h.main_control()


async def test_handshake_cancel_drains_recv_worker(h):
    h.modes[(DC, True)] = "handshake_block"
    creator = asyncio.create_task(h.media())
    await wait_until(lambda: h.transports[-1].media and h.transports[-1].request_entered.is_set())
    session = h.client.media_sessions[DC]
    h.owned.append(session)
    transport = session.connection
    recv = session.recv_task
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert DC not in h.client.media_sessions
    assert transport.closed
    assert recv.done()
    assert not session.pending_tasks
    assert not session.is_started.is_set()


@pytest.mark.parametrize("mode", ["export_error", "export_block"])
async def test_export_failure_or_cancel_does_not_stop_main(h, monkeypatch, mode):
    class FixtureAuth:
        connection = None

        def __init__(self, *args, **kwargs):
            pass

        async def create(self):
            return AUTH_KEY

    monkeypatch.setattr("pyrogram.client.Auth", FixtureAuth)
    main_transport = h.client.session.connection
    main_transport.mode = mode
    creator = asyncio.create_task(h.client.get_session(4))
    await asyncio.wait_for(main_transport.request_entered.wait(), 2)
    auxiliary = next(t for t in h.transports if t.dc_id == 4)
    if mode == "export_block":
        creator.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creator
    else:
        with pytest.raises(AuthBytesInvalid):
            await creator
    assert 4 not in h.client.sessions
    assert auxiliary.closed
    assert not main_transport.closed
    main_transport.mode = "ok"
    await h.main_control()


async def test_temporary_success_is_uncached_and_does_not_replace_shared_media(h):
    shared = await h.media()
    temporary = await h.media(temporary=True)
    assert temporary is not shared
    assert h.client.media_sessions[DC] is shared
    await temporary.stop()
    assert shared.is_started.is_set()
    assert await h.download() == FIXTURE


async def test_callback_errors_remain_nonfatal_but_logs_are_safe(h, caplog):
    caplog.set_level(logging.INFO)

    async def callback(client, session):
        raise RuntimeError("https://user:password@example.test/?token=secret +84901234567")

    h.client.connect_handler = callback
    h.client.disconnect_handler = callback
    session = await h.media()
    assert session.is_started.is_set()
    await session.stop()
    assert "phase=connect-callback" in caplog.text
    assert "phase=disconnect-callback" in caplog.text
    assert "error=RuntimeError detail=omitted" in caplog.text
    assert "password" not in caplog.text
    assert "84901234567" not in caplog.text
    assert "token=secret" not in caplog.text


async def test_safe_os_error_details_are_bounded_without_supplied_text(h, caplog):
    caplog.set_level(logging.INFO)
    h.client.name = "customer-+84901234567-secret"
    h.modes[(DC, True)] = "secret_error"
    with pytest.raises(ConnectionError):
        await h.media()
    assert "error=ConnectionError" in caplog.text
    assert "detail=Connection refused" in caplog.text
    assert "password" not in caplog.text
    assert "84901234567" not in caplog.text
    assert "token=secret" not in caplog.text


async def test_owner_and_borrower_cancel_cleanup_is_not_shared(h):
    creator, session, transport = await blocked_creator(h)
    borrowed = await h.media()
    assert borrowed is session
    waiter = asyncio.create_task(borrowed.invoke(raw.functions.help.GetConfig()))
    await asyncio.sleep(0)
    creator.cancel()
    waiter.cancel()
    results = await asyncio.gather(creator, waiter, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert transport.closed
    assert DC not in h.client.media_sessions
    await h.main_control()


async def test_direct_start_cancel_cleans_up_without_terminal_stop(h):
    session = Session(h.client, 3, "127.0.0.1", 443, AUTH_KEY, True, is_media=True)
    h.owned.append(session)
    h.modes[(3, True)] = "block"
    creator = asyncio.create_task(session.start())
    await wait_until(lambda: h.transports[-1].dc_id == 3 and h.transports[-1].entered.is_set())
    transport = session.connection
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await creator
    assert transport.closed
    assert session.state is SessionState.STOPPED
    assert not session._must_stay_stopped
    h.modes[(3, True)] = "ok"
    await session.start()
    assert session.is_started.is_set()
    await h.main_control()


async def test_connection_factory_failure_rolls_back_without_transport(h):
    original = h.client.connection_factory

    def factory(**kwargs):
        if kwargs["media"]:
            raise ValueError("fixture connection factory failed")
        return original(**kwargs)

    h.client.connection_factory = factory
    with pytest.raises(ValueError):
        await h.media()
    assert DC not in h.client.media_sessions
    h.client.connection_factory = original
    assert (await h.media()).is_started.is_set()
    assert await h.download() == FIXTURE


async def test_media_failure_does_not_stop_borrowed_control_session(h, monkeypatch):
    class FixtureAuth:
        connection = None

        def __init__(self, *args, **kwargs):
            pass

        async def create(self):
            return AUTH_KEY

    monkeypatch.setattr("pyrogram.client.Auth", FixtureAuth)
    control = await h.client.get_session(4)
    h.modes[(4, True)] = "error"
    with pytest.raises(ConnectionError):
        await h.media(4)
    assert 4 not in h.client.media_sessions
    assert h.client.sessions[4] is control
    assert control.is_started.is_set()
    assert isinstance(await control.invoke(raw.functions.help.GetConfig()), raw.types.Config)
    await h.main_control()


async def test_safe_diagnostics_discriminate_initial_cancel_and_reconnect(h, caplog):
    caplog.set_level(logging.INFO)
    h.client.name = "customer-+84901234567-secret"
    creator, _, _ = await blocked_creator(h)
    creator.cancel("https://user:password@example.test/?token=secret")
    with pytest.raises(asyncio.CancelledError):
        await creator
    h.modes[(DC, True)] = "ok"
    session = await h.media()
    h.modes[(DC, True)] = "error"
    await session.connection.incoming.put(None)
    await wait_until(
        lambda: (
            session.restart_task
            and not session.restart_task.done()
            and session.state is SessionState.STOPPED
        )
    )
    assert await h.media() is session
    await wait_until(
        lambda: any("phase=reconnect-backoff" in r.getMessage() for r in caplog.records)
    )
    h.modes[(DC, True)] = "ok"
    await asyncio.wait_for(session.restart_task, 2)
    messages = [r.getMessage() for r in caplog.records if "Session lifecycle" in r.getMessage()]
    assert any(
        "phase=reconnect-backoff" in m
        and "restart_active=True" in m
        and "attempt=1" in m
        and "retry_delay=1" in m
        for m in messages
    )
    labels = {m.split("client=", 1)[1].split()[0] for m in messages}
    assert len(labels) == 1
    assert any("phase=initial-start" in m and "error=CancelledError" in m for m in messages)
    assert all(
        "84901234567" not in m and "secret" not in m and "password" not in m for m in messages
    )
    assert any(
        "dc=5" in m
        and "media=True" in m
        and "state=" in m
        and "client=" in m
        and "attempt=" in m
        and "detail=" in m
        for m in messages
    )


@pytest.mark.parametrize("repeated_cancel", [False, True])
async def test_cancel_joins_existing_restart_stop_owner(h, repeated_cancel):
    entered = asyncio.Event()
    callback_release = asyncio.Event()

    async def callback(client, session):
        if session.is_media:
            h.owned.append(session)
            entered.set()
            await callback_release.wait()

    h.client.connect_handler = callback
    creator = asyncio.create_task(h.media())
    await asyncio.wait_for(entered.wait(), 2)
    session = h.client.media_sessions[DC]
    transport = session.connection
    recv, ping = session.recv_task, session.ping_task
    transport.block_close = True
    await transport.incoming.put(None)
    await asyncio.wait_for(transport.close_entered.wait(), 2)
    restart = session.restart_task
    count = len(h.transports)
    try:
        creator.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        if repeated_cancel:
            creator.cancel()
            await asyncio.sleep(0)
            creator.cancel()
            await asyncio.sleep(0)
        assert not creator.done(), "creator returned before the actual stop owner closed transport"
    finally:
        transport.close_release.set()
        callback_release.set()
        await asyncio.gather(creator, return_exceptions=True)
        await asyncio.wait_for(restart, 2)
    assert creator.cancelled()
    assert transport.closed
    assert transport.close_calls == 1
    assert recv.done() and ping.done()
    assert session.state is SessionState.STOPPED
    assert session._must_stay_stopped
    assert not session.pending_tasks
    assert DC not in h.client.media_sessions
    assert restart.done()
    assert len(h.transports) == count
    await h.main_control()


async def test_recv_404_during_cancel_preserves_cancel_and_drains_workers(h, caplog):
    caplog.set_level(logging.ERROR)
    h.modes[(DC, True)] = "handshake_block"
    creator = asyncio.create_task(h.media())
    await wait_until(lambda: h.transports[-1].media and h.transports[-1].request_entered.is_set())
    session = h.client.media_sessions[DC]
    h.owned.append(session)
    session.STOP_TIMEOUT = 0.01
    transport, recv = session.connection, session.recv_task
    pending = session._create_tracked_task(asyncio.Event().wait())
    await transport.incoming.put((-404).to_bytes(4, "little", signed=True))
    await wait_until(recv.done)
    creator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(creator, 2)
    assert type(recv.exception()).__name__ == "AuthKeyNotFound"
    assert transport.closed
    assert pending.cancelled()
    assert not session.pending_tasks
    assert session.recv_task is None
    assert session.state is SessionState.STOPPED
    assert not session.is_started.is_set()
    assert DC not in h.client.media_sessions
    assert "phase=cleanup-recv" in caplog.text
    assert "error=AuthKeyNotFound" in caplog.text
    await h.main_control()


@pytest.mark.parametrize("cancel_origin", [False, True])
async def test_provider_close_failure_is_explicit_without_resurrection(h, caplog, cancel_origin):
    caplog.set_level(logging.ERROR)
    if cancel_origin:
        creator, session, transport = await blocked_creator(h)
    else:
        session = await h.media()
        transport = session.connection
    original_close = transport.close
    calls = 0

    async def rejected_close():
        nonlocal calls
        calls += 1
        raise RuntimeError("provider rejected close password=hidden token=secret")

    transport.close = rejected_close
    recv = session.recv_task
    count = len(h.transports)
    try:
        if cancel_origin:
            creator.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(creator, 2)
            assert DC not in h.client.media_sessions
        else:
            with pytest.raises(RuntimeError, match="Session cleanup failed"):
                await asyncio.wait_for(session.stop(), 2)
            assert recv.done()
            assert session.recv_task is None
        assert session.state.name == "STOP_FAILED"
        assert not transport.closed
        assert session._must_stay_stopped
        assert not session.pending_tasks
        await session.start()
        with pytest.raises(RuntimeError, match="Session cleanup failed"):
            await session.restart()
        assert len(h.transports) == count
        assert calls == 1
        assert "phase=cleanup-close" in caplog.text
        assert "error=RuntimeError" in caplog.text
        assert "password=hidden" not in caplog.text
        assert "token=secret" not in caplog.text
    finally:
        transport.close = original_close
        # Restore only the fault-injected provider after the observed assertions.
        session._state = SessionState.STARTING
        await session.stop()
    await h.main_control()


async def test_provider_owned_close_timeout_is_bounded_and_recv_is_drained(h):
    session = await h.media()
    session.STOP_TIMEOUT = 0.01
    transport, recv = session.connection, session.recv_task
    transport.block_close = True
    original_close = transport.close

    async def bounded_close():
        await asyncio.wait_for(original_close(), 0.01)

    transport.close = bounded_close
    try:
        with pytest.raises(RuntimeError, match="Session cleanup failed"):
            await asyncio.wait_for(session.stop(), 2)
        assert session.state.name == "STOP_FAILED"
        assert not transport.closed
        assert recv.done()
        assert not session.is_started.is_set()
        assert session._must_stay_stopped
    finally:
        transport.close_release.set()
        session._state = SessionState.STARTING
        await session.stop()


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.TimeoutError])
async def test_ping_failure_still_closes_and_drains_independent_workers(h, caplog, error_type):
    caplog.set_level(logging.ERROR)
    session = await h.media()
    session.STOP_TIMEOUT = 0.01
    old_ping = session.ping_task
    old_ping.cancel()
    await asyncio.gather(old_ping, return_exceptions=True)
    failed_ping = asyncio.get_running_loop().create_future()
    failed_ping.set_exception(error_type("worker secret"))
    session.ping_task = failed_ping
    recv = session.recv_task
    pending = session._create_tracked_task(asyncio.Event().wait())
    disconnected = []

    async def disconnect(client, stopped):
        disconnected.append(stopped)
        await asyncio.create_task(stopped.stop())

    h.client.disconnect_handler = disconnect
    await asyncio.wait_for(session.stop(), 2)
    assert session.connection.closed
    assert recv.done()
    assert pending.cancelled()
    assert session.state is SessionState.STOPPED
    assert not session.pending_tasks
    assert disconnected == [session]
    assert "phase=cleanup-ping" in caplog.text
    assert f"error={error_type.__name__}" in caplog.text
    assert "worker secret" not in caplog.text


async def test_disconnect_callback_cross_task_stop_reentry_is_not_deadlocked(h):
    session = await h.media()
    callbacks = []

    async def callback(client, stopped):
        if stopped is session:
            callbacks.append(stopped)
            assert stopped.connection.closed
            assert stopped.recv_task is None
            await asyncio.create_task(stopped.stop())
            assert await asyncio.create_task(h.media()) is stopped

    h.client.disconnect_handler = callback
    await asyncio.wait_for(session.stop(), 2)
    assert callbacks == [session]
    assert session.state is SessionState.STOPPED


async def test_tracked_worker_stop_reentry_does_not_join_itself(h):
    session = await h.media()

    async def update_callback():
        await session.stop()

    task = session._create_tracked_task(update_callback())
    await asyncio.wait_for(task, 2)
    assert session.connection.closed
    assert session.state is SessionState.STOPPED
    await asyncio.sleep(0)
    assert not session.pending_tasks


async def test_worker_stop_request_during_external_teardown_does_not_deadlock(h):
    session = await h.media()
    transport = session.connection
    transport.block_close = True
    returned = asyncio.Event()

    async def update_callback():
        await transport.close_entered.wait()
        await session.stop()
        returned.set()

    worker = session._create_tracked_task(update_callback())
    stopping = asyncio.create_task(session.stop())
    try:
        await asyncio.wait_for(returned.wait(), 2)
        assert not stopping.done()
        assert not transport.closed
    finally:
        transport.close_release.set()
        await asyncio.wait_for(stopping, 2)
        await asyncio.wait_for(worker, 2)
    assert session.state is SessionState.STOPPED
    assert transport.closed
    assert not session.pending_tasks


@pytest.fixture
def error_consumer_log():
    # The production consumer floors both the SDK logger and handler at ERROR.
    family = logging.getLogger("pyrogram")
    children = [logging.getLogger("pyrogram.client"), logging.getLogger("pyrogram.session.session")]
    snapshots = [
        (logger, logger.level, logger.handlers[:], logger.propagate)
        for logger in [family, *children]
    ]
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.ERROR)
    family.handlers = [handler]
    family.setLevel(logging.ERROR)
    family.propagate = False
    for child in children:
        child.handlers = []
        child.setLevel(logging.NOTSET)
        child.propagate = True
    try:
        yield stream
    finally:
        for logger, level, handlers, propagate in snapshots:
            logger.setLevel(level)
            logger.handlers = handlers
            logger.propagate = propagate


async def test_terminal_failure_and_callbacks_visible_at_consumer_error_floor(
    h, error_consumer_log
):
    h.client.name = "customer +84901234567"
    h.modes[(DC, True)] = "secret_error"
    with pytest.raises(ConnectionError):
        await h.media()
    assert "phase=initial-start" in error_consumer_log.getvalue()
    assert "error=ConnectionError" in error_consumer_log.getvalue()
    h.modes[(DC, True)] = "ok"

    async def callback(client, session):
        raise RuntimeError("password=hidden token=secret +84901234567")

    h.client.connect_handler = callback
    h.client.disconnect_handler = callback
    session = await h.media()
    await session.stop()
    output = error_consumer_log.getvalue()
    assert "phase=connect-callback" in output
    assert "phase=disconnect-callback" in output
    assert "error=RuntimeError detail=omitted" in output
    assert all(secret not in output for secret in ["password", "token=secret", "+84901234567"])


@pytest.mark.parametrize(
    "packet,trigger",
    [
        (None, "trigger=recv-null trigger_code=None"),
        ((-429).to_bytes(4, "little", signed=True), "trigger=recv-transport trigger_code=429"),
    ],
)
async def test_restart_trigger_survives_backoff_at_consumer_error_floor(
    h, error_consumer_log, packet, trigger
):
    session = await h.media()
    session.RESTART_RETRY_DELAY = 0.01
    session.RESTART_RETRY_MAX_DELAY = 0.01
    h.modes[(DC, True)] = "secret_error"
    await session.connection.incoming.put(packet)
    await wait_until(lambda: "phase=reconnect-backoff" in error_consumer_log.getvalue())
    await wait_until(lambda: len(h.transports) >= 5)
    records = error_consumer_log.getvalue().splitlines()
    assert len(records) == 1, "unchanged retry failures should not spam ERROR"
    assert "error=ConnectionError detail=Connection refused" in records[0]
    assert "attempt=1" in records[0]
    assert "retry_delay=0.01" in records[0]
    assert trigger in records[0]
    assert "password" not in records[0] and "token=secret" not in records[0]
    h.modes[(DC, True)] = "error"
    await wait_until(lambda: len(error_consumer_log.getvalue().splitlines()) == 2)
    changed_failure = error_consumer_log.getvalue().splitlines()[1]
    assert "detail=omitted" in changed_failure
    assert trigger in changed_failure
    h.modes[(DC, True)] = "ok"
    await asyncio.wait_for(session.restart_task, 2)
    assert await h.download() == FIXTURE


async def test_auth_abort_visible_at_consumer_error_floor(h, error_consumer_log):
    session = await h.media()
    h.modes[(DC, True)] = "auth_error"
    await session.connection.incoming.put(None)
    await wait_until(lambda: session.restart_task is not None)
    await asyncio.wait_for(session.restart_task, 2)
    output = error_consumer_log.getvalue()
    assert "phase=reconnect-aborted" in output
    assert "error=AuthKeyUnregistered" in output
    assert "trigger=recv-null" in output
    assert session.state is SessionState.STOPPED
    assert session.connection.closed
    await h.main_control()


async def test_auth_import_error_survives_provider_cleanup_failure(h, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)

    class FixtureAuth:
        connection = None

        def __init__(self, *args, **kwargs):
            pass

        async def create(self):
            return AUTH_KEY

    monkeypatch.setattr("pyrogram.client.Auth", FixtureAuth)
    h.modes[(4, False)] = "import_error"
    owned = []
    originals = []

    async def callback(client, session):
        if session.dc_id == 4:
            owned.append(session)
            h.owned.append(session)
            originals.append(session.connection.close)

            async def rejected_close():
                raise RuntimeError("provider token=secret")

            session.connection.close = rejected_close

    h.client.connect_handler = callback
    try:
        with pytest.raises(AuthBytesInvalid):
            await asyncio.wait_for(h.client.get_session(4), 2)
        assert 4 not in h.client.sessions
        assert owned[0].state.name == "STOP_FAILED"
        assert not owned[0].connection.closed
        assert owned[0]._must_stay_stopped
        assert "phase=import-authorization" in caplog.text
        assert "error=AuthBytesInvalid" in caplog.text
        assert "phase=cleanup-close" in caplog.text
        assert "provider token=secret" not in caplog.text
    finally:
        for session, close in zip(owned, originals, strict=True):
            session.connection.close = close
            session._state = SessionState.STARTING
            await session.stop()
    await h.main_control()


@pytest.mark.parametrize("worker_fault", ["none", "pending", "recv"])
async def test_public_client_stop_finishes_after_drained_worker_fault(h, caplog, worker_fault):
    caplog.set_level(logging.ERROR)
    first = await h.media()
    second = await h.media(temporary=True)
    h.client.media_sessions[6] = second
    main = h.client.session
    first.STOP_TIMEOUT = 1
    first.connection.block_close = True
    storage_entered, release_worker = asyncio.Event(), asyncio.Event()
    calls = []

    async def save():
        calls.append("save")

    async def close():
        calls.append("close")

    async def stop_dispatcher(*, clear_handlers):
        calls.append(("dispatcher", clear_handlers))

    async def set_update_state(state):
        storage_entered.set()
        await release_worker.wait()
        if worker_fault == "pending":
            raise sqlite3.OperationalError("offline storage failure token=secret")

    h.client.storage.save = save
    h.client.storage.close = close
    h.client.storage.set_update_state = set_update_state
    h.client.dispatcher = SimpleNamespace(stop=stop_dispatcher)
    h.client.is_initialized = True
    watchdog = h.client.updates_watchdog_task = asyncio.create_task(h.client.updates_watchdog())
    updates = raw.types.Updates(updates=[], users=[], chats=[], date=0, seq=0)
    worker = first._create_tracked_task(h.client.handle_updates(updates))
    recv, ping = first.recv_task, first.ping_task
    await asyncio.wait_for(storage_entered.wait(), 2)
    if worker_fault == "recv":
        await first.connection.incoming.put((-404).to_bytes(4, "little", signed=True))
        await wait_until(recv.done)
    stopping = asyncio.create_task(h.client.stop())
    try:
        await asyncio.wait_for(first.connection.close_entered.wait(), 2)
        assert not stopping.done()
        first.connection.close_release.set()
        # `recv_task` is cleared immediately before the pending worker drain.
        await wait_until(lambda: first.recv_task is None)
        assert not stopping.done()
        assert worker in first.pending_tasks
        release_worker.set()
        assert await asyncio.wait_for(stopping, 2) is h.client
        assert all(s.connection.closed for s in (first, second, main))
        assert all(s.state is SessionState.STOPPED for s in (first, second, main))
        assert recv.done() and ping.done() and worker.done() and watchdog.done()
        assert not first.pending_tasks
        assert not h.client.media_sessions and not h.client.sessions
        assert not h.client.is_initialized and not h.client.is_connected
        assert h.client.session is None
        assert not h.client.updates_watchdog_event.is_set()
        assert calls == ["save", ("dispatcher", True), "close"]
        if worker_fault == "pending":
            assert isinstance(worker.exception(), sqlite3.OperationalError)
            assert "phase=cleanup-pending" in caplog.text
            assert "error=OperationalError" in caplog.text
        else:
            assert worker.exception() is None
        if worker_fault == "recv":
            assert type(recv.exception()).__name__ == "AuthKeyNotFound"
            assert "phase=cleanup-recv" in caplog.text
        assert "token=secret" not in caplog.text
    finally:
        first.connection.close_release.set()
        release_worker.set()
        await asyncio.gather(stopping, worker, return_exceptions=True)
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)


@pytest.mark.parametrize(
    "phase,failure", [("ping", "cancel"), ("recv", "cancel"), ("recv", "timeout")]
)
async def test_worker_drain_cancel_or_timeout_remains_explicit(h, caplog, phase, failure):
    caplog.set_level(logging.ERROR)
    session = await h.media()
    session.STOP_TIMEOUT = 0.01
    original = getattr(session, f"{phase}_task")
    original.cancel()
    await asyncio.gather(original, return_exceptions=True)
    worker = asyncio.get_running_loop().create_future()
    if failure == "cancel":
        worker.cancel()
    setattr(session, f"{phase}_task", worker)
    with pytest.raises(SessionCleanupError) as raised:
        await asyncio.wait_for(session.stop(), 2)
    expected = asyncio.CancelledError if failure == "cancel" else asyncio.TimeoutError
    assert any(p == phase and isinstance(error, expected) for p, error in raised.value.failures)
    assert worker.done() and worker.cancelled()
    assert session.connection.closed
    assert not session.pending_tasks
    assert f"phase=cleanup-{phase}" in caplog.text
    await h.main_control()


async def test_cancelled_pending_drain_is_not_a_diagnostic_worker_fault(h, caplog):
    caplog.set_level(logging.ERROR)
    session = await h.media()
    session.STOP_TIMEOUT = 1
    release_worker = asyncio.Event()
    worker = session._create_tracked_task(release_worker.wait())
    stopping = asyncio.create_task(session.stop())
    try:
        await wait_until(lambda: session.recv_task is None)
        assert worker in session.pending_tasks
        assert not stopping.done()
        session._stop_task.cancel()
        with pytest.raises(SessionCleanupError) as raised:
            await asyncio.wait_for(stopping, 2)
        assert any(
            phase == "pending" and isinstance(error, asyncio.CancelledError)
            for phase, error in raised.value.failures
        )
        assert not worker.done(), "interrupted drain cannot be classified as a finished worker"
        assert "phase=cleanup-pending" in caplog.text
        assert "error=CancelledError" in caplog.text
    finally:
        release_worker.set()
        await asyncio.gather(stopping, worker, return_exceptions=True)

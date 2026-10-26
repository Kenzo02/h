"""Real Client/Session startup, request dispatch and cleanup over an offline wire."""

from __future__ import annotations as _annotations

import asyncio
import logging
import socket
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest

from pyrogram import raw
from pyrogram.crypto import aes, mtproto
from pyrogram.errors import SessionRevoked
from pyrogram.raw.core import Message
from pyrogram.session.session import Session, SessionCleanupError, SessionState
from pyrogram.storage import SQLiteStorage
from tests.unit.session.test_auxiliary_startup import AUTH_KEY, DC, Harness, Transport, wait_until


class StartupWire(Transport):
    """Use native encryption/packet handling; substitute only provider behavior."""

    def __init__(self, owner, mode="ok"):
        super().__init__(owner, False, mode, DC)
        self.failure = None
        self.fail_query = None
        self.eof_after_config = False
        self.recv_failure = None
        self.block_query = None

    async def recv(self):
        packet = await super().recv()
        if packet is None and self.recv_failure is not None:
            raise self.recv_failure
        return packet

    async def send(self, payload):
        key, iv = mtproto.kdf(AUTH_KEY, payload[8:24], True)
        plain = BytesIO(aes.ige256_decrypt(payload[24:], key, iv))
        plain.read(8)
        session_id = plain.read(8)
        request = Message.read(plain)
        body = request.body
        if isinstance(
            body,
            (
                raw.functions.updates.GetState,
                raw.functions.users.GetFullUser,
                raw.functions.updates.GetDifference,
            ),
        ):
            self.requests.append(body.QUALNAME)
            if type(body) is self.block_query:
                self.request_entered.set()
                await self.request_release.wait()
            if type(body) is self.fail_query:
                raise self.failure
            if isinstance(body, raw.functions.updates.GetState):
                result = raw.types.updates.State(pts=1, qts=0, date=1, seq=1, unread_count=0)
            elif isinstance(body, raw.functions.updates.GetDifference):
                result = raw.types.updates.DifferenceEmpty(date=1, seq=1)
            else:
                result = raw.types.users.UserFull(
                    full_user=raw.types.UserFull(
                        id=1,
                        settings=raw.types.PeerSettings(),
                        notify_settings=raw.types.PeerNotifySettings(),
                        common_chats_count=0,
                    ),
                    chats=[],
                    users=[raw.types.User(id=1, is_self=True, first_name="Offline")],
                )
            await self.reply(
                raw.types.RpcResult(req_msg_id=request.msg_id, result=result), session_id
            )
        else:
            await super().send(payload)
            if self.eof_after_config and isinstance(body, raw.functions.InvokeWithLayer):
                # Deliver a valid handshake answer, then complete the receiver
                # before the starter resumes from that answer.
                await self.incoming.put(None)
                await wait_until(self.owner.session.recv_task.done)


@pytest.fixture
async def native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("startup regression attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(Session, "RESTART_RETRY_DELAY", 0.01)
    monkeypatch.setattr(Session, "RESTART_RETRY_MAX_DELAY", 0.02)
    h = Harness()
    c = h.client
    c.is_connected = False
    c.workers = 1
    c.skip_updates = False
    c.storage = SQLiteStorage("offline-startup", Path("."), in_memory=True)
    await c.storage.open()
    for key, value in {
        "dc_id": DC,
        "api_id": 1,
        "test_mode": True,
        "auth_key": AUTH_KEY,
        "user_id": 1,
        "is_bot": False,
        "server_address": "127.0.0.1",
        "port": 443,
    }.items():
        await getattr(c.storage, key)(value)

    async def loaded():
        # Already-authorized dummy in-memory storage, never a customer session.
        pass

    c.load_session = loaded
    c.connect_handler = None
    h.options = []

    def factory(**kwargs):
        wire = StartupWire(c)
        if h.options:
            for key, value in h.options.pop(0).items():
                setattr(wire, key, value)
        h.transports.append(wire)
        return wire

    c.connection_factory = factory
    try:
        yield h
    finally:
        c.connect_handler = c.disconnect_handler = None
        for wire in h.transports:
            wire.release.set()
            wire.close_release.set()
            wire.request_release.set()
        sessions = set(h.owned + [c.session]) - {None}
        for session in sessions:
            if session.restart_task and not session.restart_task.done():
                session.restart_task.cancel()
                await asyncio.gather(session.restart_task, return_exceptions=True)
            # Release intentionally rejected fixture closes after assertions.
            if session.state is SessionState.STOP_FAILED:
                session._state = SessionState.STARTING
            await session.stop()
        if c.is_initialized:
            await c.terminate()
        if c.updates_watchdog_task is not None:
            c.updates_watchdog_event.set()
            await c.updates_watchdog_task
        await c.dispatcher.stop()
        try:
            await c.storage.close()
        except sqlite3.ProgrammingError:
            pass
        c.executor.shutdown(wait=True)


def assert_stopped(h, session):
    assert not h.client.is_connected and not h.client.is_initialized
    assert h.client.session is None
    assert session.state is SessionState.STOPPED
    assert session._must_stay_stopped
    assert not session._has_live_workers()
    assert not h.client.dispatcher.handler_worker_tasks
    assert all(w.closed for w in h.transports)
    with pytest.raises(sqlite3.ProgrammingError):
        h.client.storage.conn.execute("SELECT 1")


async def test_public_start_healthy_then_reconnect_and_stop(native):
    c = native.client
    assert await asyncio.wait_for(c.start(), 2) is c
    session = c.session
    native.owned.append(session)
    assert c.me.id == 1 and c.is_initialized
    assert session.is_started.is_set() and not session.recv_task.done()
    await session.connection.incoming.put(None)
    await wait_until(lambda: session.restart_task is not None)
    owner = session.restart_task
    await asyncio.wait_for(owner, 2)
    assert session.restart_task is owner
    assert len(native.transports) == 2
    assert (await c.get_me()).id == 1
    await c.stop()
    assert_stopped(native, session)


@pytest.mark.parametrize(
    "query", [raw.functions.users.GetFullUser, raw.functions.updates.GetDifference]
)
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_public_start_get_me_or_initialize_failure_cleans_up(
    native, monkeypatch, query, cleanup_fails
):
    c = native.client
    primary = SessionRevoked()
    native.options = [{"fail_query": query, "failure": primary}]
    original_factory = c.connection_factory
    secondary = RuntimeError("fixture close rejected")
    rejected = []

    def factory(**kwargs):
        wire = original_factory(**kwargs)
        if cleanup_fails:
            close = wire.close

            async def reject():
                rejected.append(True)
                raise secondary

            wire.close = reject
            wire.real_close = close
        return wire

    monkeypatch.setattr(c, "connection_factory", factory)
    try:
        with pytest.raises(SessionRevoked) as caught:
            await asyncio.wait_for(c.start(), 2)
        assert caught.value is primary
        assert not c.is_initialized
        assert not c.dispatcher.handler_worker_tasks
        assert not c.session or not c.session._has_live_workers()
        assert native.transports[0].requests.count(query.QUALNAME) == 1
        if cleanup_fails:
            assert isinstance(primary.__cause__, SessionCleanupError)
            assert any(error is secondary for _, error in primary.__cause__.failures)
            assert rejected == [True]
            assert c.session.state is SessionState.STOP_FAILED
            native.owned.append(c.session)
        else:
            assert not c.is_connected and c.session is None
            assert native.transports[0].closed
        with pytest.raises(sqlite3.ProgrammingError):
            c.storage.conn.execute("SELECT 1")
    finally:
        for wire in native.transports:
            if hasattr(wire, "real_close"):
                wire.close = wire.real_close


async def test_handshake_answer_then_eof_never_returns_ready(native):
    session = native.client.session
    native.options = [{"eof_after_config": True}]
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(session.start(), 2)
    assert not session.is_started.is_set()
    assert session.state is SessionState.STOPPED
    assert native.transports[0].closed
    assert session.restart_task is None


async def test_handshake_receiver_error_retained_when_close_fails(native, monkeypatch):
    session = native.client.session
    primary = ValueError("offline receive failure")
    native.options = [{"recv_failure": primary, "eof_after_config": True}]
    original_factory = native.client.connection_factory

    def factory(**kwargs):
        wire = original_factory(**kwargs)
        original_close = wire.close

        async def rejected():
            raise RuntimeError("offline close failure")

        wire.close = rejected
        wire.real_close = original_close
        return wire

    monkeypatch.setattr(native.client, "connection_factory", factory)
    try:
        with pytest.raises(ValueError) as caught:
            await asyncio.wait_for(session.start(), 2)
        assert caught.value is primary
        assert session.state is SessionState.STOP_FAILED
        assert not session.is_started.is_set()
        assert not session._has_live_workers()
    finally:
        for wire in native.transports:
            wire.close = wire.real_close


async def test_handshake_eof_uses_existing_endpoint_fallback(native):
    session = native.client.session
    session.fallback_endpoints = (("127.0.0.1", 443), ("127.0.0.2", 443))
    native.options = [{"eof_after_config": True}, {}]
    await asyncio.wait_for(session.start(), 2)
    assert native.transports[0].closed
    assert len(native.transports) == 2
    assert session.server_address == "127.0.0.2"
    assert session.is_started.is_set() and not session.recv_task.done()
    assert isinstance(await session.invoke(raw.functions.help.GetConfig()), raw.types.Config)


@pytest.mark.parametrize("during", ["callback", "handshake"])
async def test_eof_during_restart_is_not_lost_behind_owner(native, during, caplog):
    caplog.set_level(logging.INFO)
    c = native.client
    await c.start()
    session = c.session
    native.owned.append(session)
    original = session.connection
    if during == "handshake":
        native.options = [{"eof_after_config": True}, {}]
    else:
        once = True

        async def callback(client, current):
            nonlocal once
            if once:
                once = False
                await current.connection.incoming.put(None)
                await wait_until(current.recv_task.done)
                assert not current.is_started.is_set()
                assert current.restart_task is owner

        c.connect_handler = callback
    await original.incoming.put(None)
    await wait_until(lambda: session.restart_task is not None)
    owner = session.restart_task
    await asyncio.wait_for(owner, 2)
    assert session.restart_task is owner
    assert len(native.transports) == 3
    assert all(w.closed for w in native.transports[:-1])
    assert session.is_started.is_set() and not session.recv_task.done()
    assert (await c.get_me()).id == 1
    assert "phase=reconnect-backoff" in caplog.text
    assert caplog.text.count("phase=reconnect-ready") == 1


@pytest.mark.parametrize("phase", ["connect", "callback", "backoff"])
async def test_terminal_stop_wins_restart_windows(native, phase, caplog):
    caplog.set_level(logging.INFO)
    c = native.client
    await c.start()
    session = c.session
    native.owned.append(session)
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == "connect":
        native.options = [{"mode": "block"}]
    elif phase == "backoff":
        session.RESTART_RETRY_DELAY = 0.1
        native.options = [{"mode": "error"}]
    else:

        async def callback(client, current):
            entered.set()
            await release.wait()

        c.connect_handler = callback
    await session.connection.incoming.put(None)
    await wait_until(lambda: session.restart_task is not None)
    owner = session.restart_task
    if phase == "connect":
        await wait_until(
            lambda: len(native.transports) == 2 and native.transports[-1].entered.is_set()
        )
    elif phase == "callback":
        await asyncio.wait_for(entered.wait(), 2)
    else:
        await wait_until(lambda: "phase=reconnect-backoff" in caplog.text)
    await session.stop()
    count = len(native.transports)
    release.set()
    native.transports[-1].release.set()
    await asyncio.wait_for(owner, 2)
    session.schedule_restart("late EOF")
    await session.start()
    assert session._must_stay_stopped
    assert session.state is SessionState.STOPPED
    assert not session.is_started.is_set()
    assert not session._has_live_workers()
    assert len(native.transports) == count
    assert "phase=reconnect-ready" not in caplog.text
    assert all(w.closed for w in native.transports)


@pytest.mark.parametrize(
    "query", [raw.functions.users.GetFullUser, raw.functions.updates.GetDifference]
)
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_public_start_repeated_cancel_drains_and_preserves_original(
    native, monkeypatch, query, cleanup_fails
):
    c = native.client
    native.options = [{"block_query": query, "block_close": True}]
    original_factory = c.connection_factory
    secondary = RuntimeError("offline rejected close")

    def factory(**kwargs):
        wire = original_factory(**kwargs)
        if cleanup_fails:
            wire.real_close = wire.close

            async def reject():
                wire.close_entered.set()
                await wire.close_release.wait()
                raise secondary

            wire.close = reject
        return wire

    monkeypatch.setattr(c, "connection_factory", factory)
    failures = []

    async def starter():
        try:
            await c.start()
        except asyncio.CancelledError as error:
            failures.append(error)
            raise

    starting = asyncio.create_task(starter())
    try:
        await wait_until(
            lambda: native.transports and native.transports[0].request_entered.is_set()
        )
        session, wire = c.session, native.transports[0]
        native.owned.append(session)
        starting.cancel("original startup cancellation")
        await asyncio.wait_for(wire.close_entered.wait(), 2)
        starting.cancel("later cancellation")
        await asyncio.sleep(0)
        starting.cancel("third cancellation")
        await asyncio.sleep(0)
        assert not starting.done()
        assert await c.storage.api_id() == 1
        wire.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 2)
        assert failures[0].args == ("original startup cancellation",)
        assert not session._has_live_workers()
        assert not c.is_initialized and not c.dispatcher.handler_worker_tasks
        assert wire.requests.count(query.QUALNAME) == 1
        if cleanup_fails:
            # The shared drain helper prioritizes repeated cancellation over its
            # cleanup exception; the exact provider failure remains in the owner.
            assert isinstance(failures[0].__cause__, asyncio.CancelledError)
            cleanup_error = session._stop_task.exception()
            assert isinstance(cleanup_error, SessionCleanupError)
            assert any(error is secondary for _, error in cleanup_error.failures)
            assert session.state is SessionState.STOP_FAILED
        else:
            assert_stopped(native, session)
    finally:
        for wire in native.transports:
            wire.close_release.set()
            wire.request_release.set()
            if hasattr(wire, "real_close"):
                wire.close = wire.real_close
        await asyncio.gather(starting, return_exceptions=True)


async def test_get_me_timeout_keeps_native_error_after_failed_cleanup(native, monkeypatch):
    c = native.client
    native.options = [{"fail_query": raw.functions.users.GetFullUser, "failure": TimeoutError()}]
    original_invoke = c.invoke
    original_factory = c.connection_factory
    native_errors = []

    async def bounded_invoke(query, **kwargs):
        # Change only retry budgets; execute actual Client.invoke/Session.invoke/send.
        try:
            return await original_invoke(query, retries=1, retry_delay=0, timeout=0.01, **kwargs)
        except TimeoutError as error:
            native_errors.append(error)
            raise

    def factory(**kwargs):
        wire = original_factory(**kwargs)
        wire.real_close = wire.close

        async def reject():
            raise RuntimeError("offline rejected close")

        wire.close = reject
        return wire

    monkeypatch.setattr(c, "invoke", bounded_invoke)
    monkeypatch.setattr(c, "connection_factory", factory)
    try:
        with pytest.raises(TimeoutError) as caught:
            await asyncio.wait_for(c.start(), 2)
        assert caught.value is native_errors[0]
        assert 'Failed to invoke "users.GetFullUser" after 1 retries' in str(caught.value)
        assert isinstance(caught.value.__cause__, SessionCleanupError)
        assert not c.is_initialized and not c.session._has_live_workers()
        assert native.transports[0].requests.count(raw.functions.users.GetFullUser.QUALNAME) == 1
        native.owned.append(c.session)
    finally:
        for wire in native.transports:
            wire.close = wire.real_close

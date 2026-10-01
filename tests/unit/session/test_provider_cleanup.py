"""Real provider teardown and native public lifecycle, without sockets.

Only the wire is substituted: real HTTP parsing for WEB close; native MTProto
serialization, SQLite storage, dispatcher and watchdog for Client.stop().
"""

from __future__ import annotations as _annotations

import asyncio
import logging
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyrogram import raw
from pyrogram.connection import Connection
from pyrogram.connection.transport.tcp import web_proxy_carrier
from pyrogram.connection.transport.tcp.tcp import TCP
from pyrogram.connection.transport.tcp.tcp_abridged import TCPAbridged
from pyrogram.connection.transport.tcp.web_proxy_carrier import WebProxyCarrier
from pyrogram.handlers import RawUpdateHandler
from pyrogram.session.session import Session, SessionCleanupError, SessionState, _finish_cleanup
from pyrogram.storage import SQLiteStorage
from tests.unit.session.test_auxiliary_startup import AUTH_KEY, DC, Harness, Transport, wait_until


class MemoryWriter:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.written = asyncio.Event()
        self.close_calls = 0
        self.close_error = None

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.sent.append(data)
        self.written.set()

    async def drain(self):
        pass

    def close(self):
        self.closed = True
        self.close_calls += 1

    async def wait_closed(self):
        if self.close_error is not None:
            raise self.close_error


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline lifecycle regression attempted a socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
async def web():
    carrier = WebProxyCarrier("offline.invalid", secret=bytes(16))
    up, down = MemoryWriter(), MemoryWriter()
    reader = asyncio.StreamReader()
    carrier._up._writer, carrier._up._reader = up, reader
    carrier._down._writer, carrier._down._reader = down, asyncio.StreamReader()
    carrier._session_id = "offline-fixture"
    tcp = TCP()
    tcp._web_carrier = carrier
    connection = Connection(DC, "127.0.0.1", 443, True)
    connection.protocol = tcp
    client = SimpleNamespace(name="offline-provider", disconnect_handler=None)
    session = Session(client, DC, "127.0.0.1", 443, AUTH_KEY, True)
    session.connection = connection
    session._state = SessionState.STARTED
    session.is_started.set()
    try:
        yield SimpleNamespace(
            carrier=carrier,
            up=up,
            down=down,
            reader=reader,
            tcp=tcp,
            connection=connection,
            session=session,
        )
    finally:
        # Assertions observe production state first. Explicit fixture release
        # also lets unchanged-source negative controls exit without leaks.
        reader.feed_data(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
        task = getattr(carrier, "_close_task", None)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        up.close()
        down.close()
        tcp.crypto_executor.shutdown(wait=True)


def feed_delete(web):
    web.reader.feed_data(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")


async def test_real_web_delete_uses_provider_budget_not_worker_grace(web):
    stopping = asyncio.create_task(web.session.stop())
    await asyncio.wait_for(web.up.written.wait(), 1)
    try:
        # Actual default 2s deadline from 69046d2, not an accelerated fake.
        await asyncio.sleep(2.2)
        feed_delete(web)
        await stopping
        assert web.up.closed and web.down.closed
        assert web.session.state is SessionState.STOPPED
        assert web.tcp._web_carrier is None
        assert web.up.sent[0].startswith(b"DELETE /api/v1/session/offline-fixture HTTP/1.1\r\n")
        assert len(web.up.sent) == 1
        await web.connection.close()
        await web.carrier.close()
        assert web.up.close_calls == web.down.close_calls == 1
    finally:
        feed_delete(web)
        await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.parametrize("entrypoint", ["session", "connection", "carrier"])
async def test_real_web_close_repeated_cancel_and_concurrent_owner(web, entrypoint):
    owner = getattr(web, entrypoint)
    close = owner.stop if entrypoint == "session" else owner.close
    cancellations = []

    async def caller():
        try:
            await close()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    stopping = asyncio.create_task(caller())
    await asyncio.wait_for(web.up.written.wait(), 1)
    # Direct carrier callers and the layered caller share one close operation.
    joining = asyncio.create_task(web.carrier.close())
    try:
        stopping.cancel("original cancellation")
        await asyncio.sleep(0)
        stopping.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not stopping.done()
        assert not joining.done()
        assert web.tcp._web_carrier is web.carrier
        assert not web.carrier._closed
        feed_delete(web)
        with pytest.raises(asyncio.CancelledError):
            await stopping
        # Python 3.10 Task.result() can discard the cancellation message; assert
        # the outcome at the actual coroutine boundary, before that runtime hop.
        assert cancellations[0].args == ("original cancellation",)
        await joining
        assert web.up.closed and web.down.closed
        assert web.carrier._closed
        assert len(web.up.sent) == 1
        await web.connection.close()
        await web.carrier.close()
        assert web.tcp._web_carrier is None
        assert web.up.close_calls == web.down.close_calls == 1
    finally:
        feed_delete(web)
        await asyncio.gather(stopping, joining, return_exceptions=True)


async def test_real_web_local_close_failure_is_retained_and_other_pool_closes(web, caplog):
    caplog.set_level(logging.ERROR)
    web.up.close_error = RuntimeError("fixture close failure token=secret")
    feed_delete(web)
    with pytest.raises(SessionCleanupError) as raised:
        await web.session.stop()
    assert raised.value.failures[0][0] == "close"
    assert raised.value.failures[0][1] is web.up.close_error
    assert web.down.closed
    assert web.carrier._up._writer is web.up
    assert web.tcp._web_carrier is web.carrier
    assert not web.carrier._closed
    assert web.session.state is SessionState.STOP_FAILED
    assert web.session._must_stay_stopped
    with pytest.raises(SessionCleanupError):
        await web.session.stop()
    with pytest.raises(RuntimeError, match="fixture close failure"):
        await web.connection.close()
    assert len(web.up.sent) == 1
    assert "phase=cleanup-close" in caplog.text
    assert "token=secret" not in caplog.text


async def test_real_web_delete_provider_timeout_closes_both_pools(web, monkeypatch):
    # Accelerate the real HTTP request budget, keeping native retry/parsing.
    monkeypatch.setattr(web_proxy_carrier, "_REQUEST_ATTEMPTS", 1)
    original_request = web.carrier._up.request

    async def short_request(method, **kwargs):
        return await original_request(method, timeout=0.01, **kwargs)

    monkeypatch.setattr(web.carrier._up, "request", short_request)
    await web.session.stop()
    assert web.up.closed and web.down.closed
    assert web.tcp._web_carrier is None
    assert web.session.state is SessionState.STOPPED
    assert len(web.up.sent) == 1


async def test_real_web_local_close_deadline_is_explicit_and_retains_owner(web, monkeypatch):
    monkeypatch.setattr(web_proxy_carrier, "_CONNECT_TIMEOUT", 0.01)
    wait_entered = asyncio.Event()

    async def blocked_wait_closed():
        wait_entered.set()
        await asyncio.Event().wait()

    # Substitute only the wire's closure acknowledgement, not provider close.
    monkeypatch.setattr(web.up, "wait_closed", blocked_wait_closed)
    feed_delete(web)
    with pytest.raises(SessionCleanupError) as raised:
        await web.session.stop()
    assert wait_entered.is_set()
    assert any(
        phase == "close" and isinstance(error, asyncio.TimeoutError)
        for phase, error in raised.value.failures
    )
    assert web.down.closed
    assert web.carrier._up._writer is web.up
    assert web.tcp._web_carrier is web.carrier
    assert not web.carrier._closed
    assert web.session.state is SessionState.STOP_FAILED
    with pytest.raises(asyncio.TimeoutError):
        await web.connection.close()
    assert len(web.up.sent) == 1


@pytest.fixture
async def sdk():
    harness = Harness()
    c = harness.client
    # The shared wire harness supplies an endpoint helper for other tests.
    # Restore the native API here and select the offline endpoint explicitly.
    del c.get_dc_option
    c.workers = 1
    c.storage = SQLiteStorage("native-stop", Path("."), in_memory=True)
    await c.storage.open()
    await c.storage.dc_id(DC)
    await c.storage.api_id(1)
    await c.storage.test_mode(True)
    await c.storage.auth_key(AUTH_KEY)
    await c.storage.user_id(1)
    await c.storage.is_bot(False)
    await c.session.start()
    await c.initialize()
    first = await harness.media(server_address="127.0.0.1", port=443)
    second = await harness.media(temporary=True, server_address="127.0.0.1", port=443)
    c.media_sessions[6] = second
    auxiliary = await c.get_session(
        DC, export_authorization=False, temporary=True, server_address="127.0.0.1", port=443
    )
    c.sessions[DC] = auxiliary
    sessions = (first, second, auxiliary, c.session)
    try:
        yield SimpleNamespace(h=harness, client=c, first=first, sessions=sessions)
    finally:
        c.connect_handler = c.disconnect_handler = None
        c.is_connected = False
        for transport in harness.transports:
            for name in ("release", "close_release", "import_release", "request_release"):
                getattr(transport, name).set()
        for session in sessions:
            if session.restart_task and not session.restart_task.done():
                session.restart_task.cancel()
                await asyncio.gather(session.restart_task, return_exceptions=True)
            stop_task = getattr(session, "_stop_task", None)
            if stop_task is not None and not stop_task.done():
                await asyncio.gather(stop_task, return_exceptions=True)
            # Unchanged-source negative controls can leave partial teardown.
            if session.state.name in {"STOPPING", "STOP_FAILED"}:
                session._state = SessionState.STARTING
            await session.stop()
        c.updates_watchdog_event.set()
        assert c.updates_watchdog_task is not None
        await asyncio.gather(c.updates_watchdog_task, return_exceptions=True)
        await c.dispatcher.stop()
        await c.storage.close()
        c.executor.shutdown(wait=True)


def assert_native_stopped(sdk):
    c = sdk.client
    assert all(s.connection.closed for s in sdk.sessions)
    assert all(s.state is SessionState.STOPPED for s in sdk.sessions)
    assert all(not s.pending_tasks for s in sdk.sessions)
    assert all(
        task is None or task.done()
        for s in sdk.sessions
        for task in (s.ping_task, s.recv_task, s.restart_task)
    )
    assert not c.media_sessions and not c.sessions
    assert not c.is_initialized and not c.is_connected
    assert c.session is None
    assert c.updates_watchdog_task.done()
    assert all(task.done() for task in c.dispatcher.handler_worker_tasks)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        c.storage.conn.execute("SELECT 1")


async def test_public_stop_real_ping_send_longer_than_worker_grace(sdk, monkeypatch):
    first = sdk.first
    first.ping_task.cancel()
    await asyncio.gather(first.ping_task, return_exceptions=True)
    entered = asyncio.Event()
    original_send = first.connection.send
    first_send = True

    async def slow_wire(payload):
        nonlocal first_send
        if first_send:
            first_send = False
            entered.set()
            await asyncio.sleep(2.2)
        await original_send(payload)

    monkeypatch.setattr(first.connection, "send", slow_wire)
    first.PING_INTERVAL = 0.001
    first.ping_task = asyncio.create_task(first.ping_worker())
    await asyncio.wait_for(entered.wait(), 1)
    assert await sdk.client.stop(clear_handlers=False) is sdk.client
    assert "functions.PingDelayDisconnect" in first.connection.requests
    assert_native_stopped(sdk)


@pytest.mark.parametrize("fail_main", [False, True])
async def test_public_stop_close_failure_settles_independent_resources(
    sdk, monkeypatch, caplog, fail_main
):
    caplog.set_level(logging.ERROR)
    failed = [sdk.first]
    if fail_main:
        failed.append(sdk.sessions[-1])
    originals = [(s.connection, s.connection.close) for s in failed]

    async def reject_close():
        raise RuntimeError("fixture provider rejection token=secret")

    for transport, _original in originals:
        monkeypatch.setattr(transport, "close", reject_close)
    try:
        with pytest.raises(SessionCleanupError) as raised:
            await sdk.client.stop()
        assert len(
            [phase for phase, _ in raised.value.failures if phase.endswith("-close")]
        ) == len(failed)
        assert all(s.connection.closed for s in sdk.sessions if s not in failed)
        assert all(s.state is SessionState.STOP_FAILED for s in failed)
        assert all(not s.connection.closed for s in failed)
        assert all(not s._has_live_workers() for s in sdk.sessions)
        assert sdk.client.media_sessions == {DC: sdk.first}
        assert not sdk.client.sessions
        assert not sdk.client.is_initialized
        assert sdk.client.updates_watchdog_task.done()
        assert not sdk.client.dispatcher.handler_worker_tasks
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            sdk.client.storage.conn.execute("SELECT 1")
        assert "token=secret" not in caplog.text
        for session in failed:
            with pytest.raises(SessionCleanupError):
                await session.restart()
    finally:
        for transport, original in originals:
            monkeypatch.setattr(transport, "close", original)


async def test_public_stop_repeated_cancel_keeps_storage_until_worker_settles(sdk):
    first = sdk.first
    first.STOP_TIMEOUT = 1
    first.connection.block_close = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def storage_worker():
        entered.set()
        await release.wait()
        assert await sdk.client.storage.api_id() == 1

    worker = first._create_tracked_task(storage_worker())
    await entered.wait()
    cancellations = []

    async def caller():
        try:
            await sdk.client.stop()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    stopping = asyncio.create_task(caller())
    try:
        await asyncio.wait_for(first.connection.close_entered.wait(), 1)
        stopping.cancel("original stop cancellation")
        await asyncio.sleep(0)
        stopping.cancel("second stop cancellation")
        await asyncio.sleep(0)
        assert not stopping.done()
        assert await sdk.client.storage.api_id() == 1
        first.connection.close_release.set()
        await wait_until(lambda: first.recv_task is None)
        assert worker in first.pending_tasks
        assert not stopping.done()
        assert await sdk.client.storage.api_id() == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert cancellations[0].args == ("original stop cancellation",)
        assert worker.done() and worker.exception() is None
        assert_native_stopped(sdk)
    finally:
        first.connection.close_release.set()
        release.set()
        await asyncio.gather(stopping, worker, return_exceptions=True)


async def test_public_stop_failed_drain_retains_storage_and_disconnect_owner(sdk):
    first = sdk.first
    first.STOP_TIMEOUT = 1
    entered, release = asyncio.Event(), asyncio.Event()

    async def storage_worker():
        entered.set()
        await release.wait()
        assert await sdk.client.storage.api_id() == 1

    worker = first._create_tracked_task(storage_worker())
    await entered.wait()
    stopping = asyncio.create_task(sdk.client.stop())
    try:
        await wait_until(lambda: first.recv_task is None)
        assert worker in first.pending_tasks
        # A genuine interrupted drain must not make storage appear safe merely
        # because independent transports can now be closed.
        first._stop_task.cancel()
        with pytest.raises(SessionCleanupError) as raised:
            await stopping
        assert any(phase == "main-workers" for phase, _ in raised.value.failures)
        assert sdk.client.session is sdk.sessions[-1]
        assert sdk.client.media_sessions == {DC: first}
        assert sdk.client.is_connected and not sdk.client.is_initialized
        assert all(s.connection.closed for s in sdk.sessions)
        assert not worker.done()
        assert await sdk.client.storage.api_id() == 1
        release.set()
        await worker
        await asyncio.sleep(0)
        # Storage remains explicitly owned until the public retry can close it.
        await sdk.client.disconnect()
        assert sdk.client.session is None and not sdk.client.is_connected
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            sdk.client.storage.conn.execute("SELECT 1")
    finally:
        release.set()
        await asyncio.gather(stopping, worker, return_exceptions=True)


async def test_public_stop_cancel_preserves_origin_despite_close_failure(sdk, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    transport = sdk.first.connection
    original_close = transport.close
    entered, release = asyncio.Event(), asyncio.Event()
    cancellations = []

    async def rejected_close():
        entered.set()
        await release.wait()
        raise RuntimeError("fixture rejection token=secret")

    async def caller():
        try:
            await sdk.client.stop()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    monkeypatch.setattr(transport, "close", rejected_close)
    stopping = asyncio.create_task(caller())
    try:
        await entered.wait()
        stopping.cancel("original cancellation")
        await asyncio.sleep(0)
        stopping.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert cancellations[0].args == ("original cancellation",)
        assert sdk.first.state is SessionState.STOP_FAILED
        assert sdk.client.media_sessions == {DC: sdk.first}
        assert all(s.connection.closed for s in sdk.sessions[1:])
        assert all(not s._has_live_workers() for s in sdk.sessions)
        assert not sdk.client.is_initialized and not sdk.client.is_connected
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            sdk.client.storage.conn.execute("SELECT 1")
        assert "phase=cleanup-close" in caplog.text
        assert "token=secret" not in caplog.text
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)
        monkeypatch.setattr(transport, "close", original_close)


class NativeWire(MemoryWriter):
    """Only the byte stream is substituted; framing and MTProto remain native."""

    def __init__(self, client, reader):
        super().__init__()
        self.reader = reader
        self.transport = SimpleNamespace(abort=reader.feed_eof)
        self.server = Transport(client, False, "ok", DC)
        self.frames = asyncio.Queue()
        self.drain_entered = asyncio.Event()
        self.drain_release = asyncio.Event()
        self.stalled = False
        self.delay = 0
        self.drain_cancelled = False

    def write(self, data):
        super().write(data)
        self.frames.put_nowait(data)

    async def drain(self):
        self.drain_entered.set()
        try:
            if self.stalled:
                await self.drain_release.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.drain_cancelled = True
            raise
        frame = self.frames.get_nowait()
        size = 4 if frame[0] == 0x7F else 1
        words = int.from_bytes(frame[1:4], "little") if size == 4 else frame[0]
        assert len(frame[size:]) == words * 4
        await self.server.send(frame[size:])
        while not self.server.incoming.empty():
            packet = self.server.incoming.get_nowait()
            words = len(packet) // 4
            prefix = bytes([words]) if words < 127 else b"\x7f" + words.to_bytes(3, "little")
            self.reader.feed_data(prefix + packet)

    def close(self):
        super().close()
        self.reader.feed_eof()


@pytest.fixture
async def native_sdk(sdk):
    # Enter the established-session boundary without sockets or authentication.
    # Replace all four connections, not Session/Client/storage/dispatcher methods.
    wires = []
    for session in sdk.sessions:
        for task in (session.ping_task, session.recv_task):
            task.cancel()
        await asyncio.gather(session.ping_task, session.recv_task, return_exceptions=True)
        await session.connection.close()
        connection = Connection(DC, "127.0.0.1", 443, True)
        tcp = connection.protocol = TCPAbridged()
        tcp.reader = asyncio.StreamReader()
        wire = tcp.writer = NativeWire(sdk.client, tcp.reader)
        tcp.marker_event.set()
        wires.append(wire)
        session.connection = connection
        session.recv_task = asyncio.create_task(session.recv_worker())
        session.ping_task = asyncio.create_task(session.ping_worker())
        # Prove this boundary really serializes, frames and receives a response.
        assert isinstance(await session.send(raw.functions.Ping(ping_id=1)), raw.types.Pong)
        wire.drain_entered.clear()
        # Keep periodic salt refresh out of tests of a single in-flight ping.
        session._future_salts_requested_at = session.client.server_time
    sdk.wires = wires
    try:
        yield sdk
    finally:
        for wire in wires:
            wire.drain_release.set()
        for session in sdk.sessions:
            await session.stop()
            session.connection.protocol.crypto_executor.shutdown(wait=True)


def assert_native_wire_stopped(sdk):
    assert all(wire.closed for wire in sdk.wires)
    assert all(s.connection.protocol.writer is None for s in sdk.sessions)
    assert all(s.state is SessionState.STOPPED for s in sdk.sessions)
    assert all(not s._has_live_workers() for s in sdk.sessions)
    assert not sdk.client.is_initialized and not sdk.client.is_connected
    assert sdk.client.session is None
    assert sdk.client.updates_watchdog_task.done()
    assert not sdk.client.dispatcher.handler_worker_tasks
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        sdk.client.storage.conn.execute("SELECT 1")


@pytest.mark.parametrize("role", ["tracked", "recv", "ping"])
@pytest.mark.parametrize("session_index", [0, 2, 3], ids=["media", "auxiliary", "main"])
@pytest.mark.parametrize("cancel", [False, True], ids=["ordinary", "repeated-cancel"])
async def test_native_worker_public_self_stop_retains_origin_until_disconnect(
    native_sdk, monkeypatch, role, session_index, cancel
):
    sdk = native_sdk
    c = sdk.client
    session = sdk.sessions[session_index]
    returned, release = asyncio.Event(), asyncio.Event()
    independent_entered, independent_release = asyncio.Event(), asyncio.Event()
    errors = []
    origins = []

    async def independent():
        independent_entered.set()
        await independent_release.wait()
        assert await c.storage.api_id() == 1

    sibling = sdk.sessions[1]._create_tracked_task(independent())
    await independent_entered.wait()

    async def request_stop():
        origins.append(asyncio.current_task())
        try:
            await c.stop()
        except (SessionCleanupError, asyncio.CancelledError) as error:
            errors.append(error)
        returned.set()
        # A returned self-stop is not permission to close storage behind us.
        assert await c.storage.api_id() == 1
        await release.wait()
        assert await c.storage.api_id() == 1

    if role == "tracked":
        worker = session._create_tracked_task(request_stop())
    elif role == "recv":
        session.recv_task.cancel()
        await asyncio.gather(session.recv_task, return_exceptions=True)
        original_recv = session.connection.recv

        async def receiving():
            await request_stop()
            return await original_recv()

        monkeypatch.setattr(session.connection, "recv", receiving)
        worker = session.recv_task = asyncio.create_task(session.recv_worker())
    else:
        session.ping_task.cancel()
        await asyncio.gather(session.ping_task, return_exceptions=True)
        original_send = session.connection.send

        async def sending(payload):
            await request_stop()
            return await original_send(payload)

        monkeypatch.setattr(session.connection, "send", sending)
        session.PING_INTERVAL = 0.001
        worker = session.ping_task = asyncio.create_task(session.ping_worker())
    try:
        await wait_until(lambda: sdk.sessions[1].state is SessionState.STOPPING)
        assert not returned.is_set()
        assert not sibling.done()
        assert await c.storage.api_id() == 1
        if cancel:
            worker.cancel("original self-stop cancellation")
            await asyncio.sleep(0)
            worker.cancel("second self-stop cancellation")
            await asyncio.sleep(0)
            assert not returned.is_set()
            assert not worker.done()
        independent_release.set()
        await asyncio.wait_for(returned.wait(), 1)
        assert origins == [worker]
        assert len(errors) == 1
        if cancel:
            assert isinstance(errors[0], asyncio.CancelledError)
            assert errors[0].args == ("original self-stop cancellation",)
        else:
            assert any(phase == "main-workers" for phase, _ in errors[0].failures)
        assert sibling.done() and sibling.exception() is None
        assert session._has_live_workers() and not worker.done()
        if role == "tracked":
            assert worker in session.pending_tasks
        else:
            assert getattr(session, f"{role}_task") is worker
        if session_index == 0:
            assert c.media_sessions == {DC: session}
        elif session_index == 2:
            assert c.sessions == {DC: session}
        assert c.session is sdk.sessions[-1]
        assert c.is_connected and not c.is_initialized
        assert all(w.closed for w in sdk.wires)
        assert all(s._must_stay_stopped for s in sdk.sessions)
        release.set()
        await asyncio.wait_for(asyncio.shield(worker), 1)
        await asyncio.sleep(0)
        assert not session._has_live_workers()
        assert origins == [worker]
        assert session.restart_task is None
        await session.restart()
        assert session.state is SessionState.STOPPED
        assert session.connection.protocol.writer is None
        assert await c.storage.api_id() == 1
        await c.disconnect()
        assert not c.media_sessions and not c.sessions
        assert_native_wire_stopped(sdk)
    finally:
        independent_release.set()
        release.set()
        await asyncio.gather(worker, sibling, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_native_stalled_tcp_drain_bounds_public_stop(native_sdk, monkeypatch, cancel):
    sdk = native_sdk
    session, wire = sdk.first, sdk.wires[0]
    # Accelerate only the provider-owned deadline, not Session's worker grace.
    monkeypatch.setattr(TCP, "TIMEOUT", 0.3)
    session.ping_task.cancel()
    await asyncio.gather(session.ping_task, return_exceptions=True)
    wire.stalled = True
    session.PING_INTERVAL = 0.001
    ping = session.ping_task = asyncio.create_task(session.ping_worker())
    await asyncio.wait_for(wire.drain_entered.wait(), 1)
    cancellations = []

    async def stop():
        try:
            await sdk.client.stop()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    stopping = asyncio.create_task(stop())
    try:
        await wait_until(lambda: session.state is SessionState.STOPPING)
        if cancel:
            stopping.cancel("original native stop cancellation")
            await asyncio.sleep(0)
            stopping.cancel("second native stop cancellation")
            await asyncio.sleep(0)
        assert not stopping.done()
        assert not wire.closed
        assert await sdk.client.storage.api_id() == 1
        done, _ = await asyncio.wait({stopping}, timeout=1)
        assert done == {stopping}, "native writer.drain outlived its provider budget"
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert cancellations[0].args == ("original native stop cancellation",)
        else:
            await stopping
        assert wire.drain_cancelled and not wire.drain_release.is_set()
        assert ping.done() and not ping.cancelled()
        assert_native_wire_stopped(sdk)
    finally:
        wire.drain_release.set()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_native_healthy_tcp_drain_exceeds_worker_grace(native_sdk):
    sdk = native_sdk
    session, wire = sdk.first, sdk.wires[0]
    session.ping_task.cancel()
    await asyncio.gather(session.ping_task, return_exceptions=True)
    wire.delay = 2.2
    session.PING_INTERVAL = 0.001
    ping = session.ping_task = asyncio.create_task(session.ping_worker())
    await asyncio.wait_for(wire.drain_entered.wait(), 1)
    assert await sdk.client.stop() is sdk.client
    assert not wire.drain_cancelled
    assert "functions.PingDelayDisconnect" in wire.server.requests
    assert ping.done() and not ping.cancelled()
    assert_native_wire_stopped(sdk)


async def test_native_dispatcher_nonblocking_stop_fully_drains_worker(native_sdk):
    sdk = native_sdk
    c = sdk.client
    returned, release = asyncio.Event(), asyncio.Event()
    workers = list(c.dispatcher.handler_worker_tasks)

    async def handler(client, update, users, chats):
        assert await client.stop(block=False) is client
        returned.set()
        await release.wait()
        assert await c.storage.api_id() == 1

    c.dispatcher.add_handler(RawUpdateHandler(handler), 0)
    c.dispatcher.updates_queue.put_nowait(
        (raw.types.UpdateDeleteMessages(messages=[1], pts=1, pts_count=1), {}, {})
    )
    try:
        await asyncio.wait_for(returned.wait(), 1)
        await asyncio.sleep(0)
        assert all(not worker.done() for worker in workers)
        assert not any(wire.closed for wire in sdk.wires)
        assert await c.storage.api_id() == 1
        release.set()
        await wait_until(lambda: not c.is_connected)
        assert all(worker.done() for worker in workers)
        assert_native_wire_stopped(sdk)
    finally:
        release.set()


@pytest.mark.parametrize("nested_cleanup", [False, True])
async def test_native_nonblocking_stop_does_not_exclude_requesting_worker(
    native_sdk, nested_cleanup
):
    sdk = native_sdk
    returned, release = asyncio.Event(), asyncio.Event()

    async def request_stop():
        assert await sdk.client.stop(block=False) is sdk.client

    async def worker_body():
        if nested_cleanup:
            # A detached do_it inherits this helper's context, but its original
            # caller is not waiting for the detached cleanup and must be drained.
            await _finish_cleanup(request_stop())
        else:
            await request_stop()
        returned.set()
        await release.wait()
        assert await sdk.client.storage.api_id() == 1

    worker = sdk.first._create_tracked_task(worker_body())
    try:
        await asyncio.wait_for(returned.wait(), 1)
        await wait_until(lambda: sdk.first.recv_task is None)
        assert not worker.done() and worker in sdk.first.pending_tasks
        assert sdk.first.state is SessionState.STOPPING
        assert await sdk.client.storage.api_id() == 1
        assert not sdk.wires[-1].closed
        release.set()
        await worker
        await wait_until(lambda: not sdk.client.is_connected)
        assert not sdk.client.media_sessions and not sdk.client.sessions
        assert_native_wire_stopped(sdk)
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)

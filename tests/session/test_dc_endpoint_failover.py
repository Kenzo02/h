import asyncio
import base64
import logging
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyrogram import Client
from pyrogram.dc_options import get_dc_endpoints
from pyrogram.session import Auth, Session
from pyrogram.storage.sqlite_storage import SQLiteStorage
from pyrogram.storage.storage import Storage


DC5_PRIMARY = "91.108.56.130"
DC5_FALLBACK = "149.154.171.5"


def encode_session_string(fmt, *values):
    return base64.urlsafe_b64encode(struct.pack(fmt, *values)).decode().rstrip("=")


def test_dc_endpoint_helper_keeps_test_mode_single_endpoint():
    assert get_dc_endpoints(3, True) == (("149.154.175.117", 80),)


@pytest.mark.asyncio
async def test_old_dc5_session_string_sets_dc5_endpoint(tmp_path: Path):
    auth_key = b"o" * 256
    session_string = encode_session_string(
        Storage.OLD_SESSION_STRING_FORMAT_64,
        5,
        False,
        auth_key,
        1234567890123,
        False,
    )
    storage = SQLiteStorage("old-dc5", tmp_path, session_string=session_string, in_memory=True)

    await storage.open()

    try:
        assert await storage.dc_id() == 5
        assert await storage.server_address() == DC5_PRIMARY
        assert await storage.port() == 443
        assert await storage.auth_key() == auth_key
        assert await storage.user_id() == 1234567890123
        assert not await storage.is_bot()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_dc5_string_session_client_connect_falls_back_same_dc(monkeypatch, tmp_path: Path):
    attempts = []
    auth_key = b"n" * 256
    session_string = encode_session_string(
        Storage.SESSION_STRING_FORMAT,
        5,
        12345,
        False,
        auth_key,
        987654321,
        False,
    )
    failures = {(DC5_PRIMARY, 443): OSError("timed out")}

    def connection_factory(**kwargs):
        return FailingConnection(attempts, failures, **kwargs)

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)

    client = Client(
        "dc5-string",
        session_string=session_string,
        workdir=tmp_path,
        connection_factory=connection_factory,
    )

    try:
        assert await client.connect() is True
        assert attempts == [(DC5_PRIMARY, 443), (DC5_FALLBACK, 443)]
        assert await client.storage.dc_id() == 5
        assert await client.storage.auth_key() == auth_key
        assert await client.storage.user_id() == 987654321
        assert await client.storage.server_address() == DC5_FALLBACK
        assert await client.storage.port() == 443
        assert client.session.server_address == DC5_FALLBACK
        assert client.session.port == 443
        assert client.session.fallback_endpoints[0] == (DC5_FALLBACK, 443)
    finally:
        if client.is_connected:
            await client.disconnect()


class FakeStorage:
    async def api_id(self):
        return 12345


class FakeAuthClient:
    app_version = "test"
    device_model = "test"
    ipv6 = False
    loop = None
    protocol_factory = None
    proxy = None
    server_time = 0
    system_version = "test"

    def __init__(self, loop, attempts, failures):
        self.loop = loop

        def connection_factory(**kwargs):
            return FailingConnection(attempts, failures, **kwargs)

        self.connection_factory = connection_factory


class FailingConnection:
    def __init__(self, attempts, failures, **kwargs):
        self.attempts = attempts
        self.failures = failures
        self.server_address = kwargs["server_address"]
        self.port = kwargs["port"]
        self.attempts.append((self.server_address, self.port))

    async def connect(self):
        failure = self.failures.get((self.server_address, self.port))

        if failure:
            raise failure

    async def close(self):
        return None


def make_client(loop, attempts, failures):
    def connection_factory(**kwargs):
        return FailingConnection(attempts, failures, **kwargs)

    return SimpleNamespace(
        app_version="test",
        connect_handler=None,
        disconnect_handler=None,
        device_model="test",
        init_connection_params=None,
        lang_code="en",
        lang_pack="",
        loop=loop,
        name="test-client",
        protocol_factory=None,
        proxy=None,
        storage=FakeStorage(),
        system_lang_code="en",
        system_version="test",
        connection_factory=connection_factory,
    )


@pytest.mark.asyncio
async def test_dc5_session_start_falls_back_to_same_dc_endpoint(monkeypatch, caplog):
    attempts = []
    failures = {(DC5_PRIMARY, 443): OSError("timed out")}
    client = make_client(asyncio.get_running_loop(), attempts, failures)
    send_calls = []

    async def fake_send(self, query, *args, **kwargs):
        send_calls.append((self.server_address, query.__class__.__name__))
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    caplog.set_level(logging.INFO)

    session = Session(
        client,
        5,
        DC5_PRIMARY,
        443,
        b"s" * 256,
        False,
        fallback_endpoints=((DC5_PRIMARY, 443), (DC5_FALLBACK, 443)),
    )

    await session.start()

    try:
        assert attempts == [(DC5_PRIMARY, 443), (DC5_FALLBACK, 443)]
        assert session.server_address == DC5_FALLBACK
        assert session.port == 443
        assert session.fallback_endpoints[0] == (DC5_FALLBACK, 443)
        assert send_calls
        assert "DC5" in caplog.text
        assert DC5_PRIMARY in caplog.text
        assert DC5_FALLBACK in caplog.text
        assert "OSError" in caplog.text
        assert (b"s" * 256).hex() not in caplog.text
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_session_start_raises_when_all_endpoints_fail(monkeypatch):
    attempts = []
    failures = {
        (DC5_PRIMARY, 443): OSError("timed out"),
        (DC5_FALLBACK, 443): OSError("timed out"),
    }
    client = make_client(asyncio.get_running_loop(), attempts, failures)

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("startup should not send MTProto requests after connect failures")

    monkeypatch.setattr(Session, "send", fail_if_called)

    session = Session(
        client,
        5,
        DC5_PRIMARY,
        443,
        b"s" * 256,
        False,
        fallback_endpoints=((DC5_PRIMARY, 443), (DC5_FALLBACK, 443)),
    )

    with pytest.raises(ConnectionError):
        await session.start()

    assert attempts == [(DC5_PRIMARY, 443), (DC5_FALLBACK, 443)]
    assert not session.is_started.is_set()


@pytest.mark.asyncio
async def test_auth_create_falls_back_before_failing():
    attempts = []
    failures = {
        (DC5_PRIMARY, 443): ConnectionError("timed out"),
        (DC5_FALLBACK, 443): ConnectionError("timed out"),
    }
    client = FakeAuthClient(asyncio.get_running_loop(), attempts, failures)
    auth = Auth(
        client,
        5,
        DC5_PRIMARY,
        443,
        False,
        fallback_endpoints=((DC5_PRIMARY, 443), (DC5_FALLBACK, 443)),
    )

    with pytest.raises(ConnectionError):
        await auth.create()

    assert attempts == [(DC5_PRIMARY, 443), (DC5_FALLBACK, 443)]

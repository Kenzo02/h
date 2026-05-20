import asyncio
import base64
import json
import logging
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyrogram import Client, raw
from pyrogram.dc_options import get_dc_endpoints
from pyrogram.session import Auth, Session
from pyrogram.storage.sqlite_storage import SQLiteStorage
from pyrogram.storage.storage import Storage


DC5_PRIMARY = "91.108.56.130"
DC5_FALLBACK = "149.154.171.5"
DC5_CONFIG_ENDPOINT = "91.108.56.194"


def encode_session_string(fmt, *values):
    return base64.urlsafe_b64encode(struct.pack(fmt, *values)).decode().rstrip("=")


def test_dc_endpoint_helper_keeps_test_mode_single_endpoint():
    assert get_dc_endpoints(3, True) == (("149.154.175.117", 80),)


def test_dc_endpoint_helper_has_same_dc_prod_fallbacks_for_known_builtin_options():
    assert get_dc_endpoints(1, False) == (
        ("149.154.175.53", 443),
        ("149.154.175.50", 443),
    )
    assert get_dc_endpoints(2, False) == (
        ("149.154.167.51", 443),
        ("95.161.76.100", 443),
    )
    assert get_dc_endpoints(5, False) == (
        (DC5_PRIMARY, 443),
        (DC5_FALLBACK, 443),
    )


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
    probes = []
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
    failures = {}

    def connection_factory(**kwargs):
        return FailingConnection(attempts, failures, **kwargs)

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))

        if server_address == DC5_PRIMARY:
            return False, 0.250, TimeoutError("timed out")

        return True, 0.020, None

    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: tmp_path / "empty-cache.json")
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    client = Client(
        "dc5-string",
        session_string=session_string,
        workdir=tmp_path,
        connection_factory=connection_factory,
    )

    try:
        assert await client.connect() is True
        assert probes == [(DC5_PRIMARY, 443), (DC5_FALLBACK, 443)]
        assert attempts == [(DC5_FALLBACK, 443)]
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


@pytest.mark.asyncio
async def test_dc5_string_session_uses_cached_same_dc_endpoint_before_probing(monkeypatch, tmp_path: Path):
    attempts = []
    probes = []
    cache_file = tmp_path / "dc-endpoints.json"
    cache_file.write_text(json.dumps({
        "version": 1,
        "endpoints": {
            "prod:v4:dc5:api": {
                "server_address": DC5_FALLBACK,
                "port": 443,
                "updated_at": 1,
            },
        },
    }))
    auth_key = b"c" * 256
    session_string = encode_session_string(
        Storage.SESSION_STRING_FORMAT,
        5,
        12345,
        False,
        auth_key,
        987654321,
        False,
    )

    def connection_factory(**kwargs):
        return FailingConnection(attempts, {}, **kwargs)

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))
        return True, 0.020, None

    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: cache_file)
    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    client = Client(
        "dc5-cache",
        session_string=session_string,
        workdir=tmp_path,
        connection_factory=connection_factory,
    )

    try:
        assert await client.connect() is True
        assert attempts == [(DC5_FALLBACK, 443)]
        assert probes == []
    finally:
        if client.is_connected:
            await client.disconnect()


@pytest.mark.asyncio
async def test_startup_stored_custom_endpoint_is_not_preempted_by_static_fallback(monkeypatch, tmp_path: Path):
    attempts = []
    probes = []
    custom_address = "203.0.113.10"
    client = make_loaded_client(asyncio.get_running_loop(), attempts, {})

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))
        return True, 0.010, None

    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: tmp_path / "dc-cache.json")
    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    session = await client.get_session(
        dc_id=5,
        server_address=custom_address,
        port=443,
        temporary=True,
        export_authorization=False,
        order_fallback_endpoints=True,
    )

    try:
        assert attempts == [(custom_address, 443)]
        assert probes == []
        assert session.server_address == custom_address
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_startup_stored_custom_endpoint_failure_does_not_try_static_fallback(monkeypatch, tmp_path: Path):
    attempts = []
    custom_address = "203.0.113.10"
    client = make_loaded_client(
        asyncio.get_running_loop(),
        attempts,
        {(custom_address, 443): OSError("custom endpoint down")},
    )

    async def fail_probe(*args, **kwargs):
        raise AssertionError("custom startup endpoint must not use endpoint probes")

    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: tmp_path / "dc-cache.json")
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fail_probe)

    with pytest.raises(ConnectionError):
        await client.get_session(
            dc_id=5,
            server_address=custom_address,
            port=443,
            temporary=True,
            export_authorization=False,
            order_fallback_endpoints=True,
        )

    assert attempts == [(custom_address, 443)]


@pytest.mark.asyncio
async def test_dc5_successful_fallback_updates_endpoint_cache(monkeypatch, tmp_path: Path):
    attempts = []
    cache_file = tmp_path / "dc-endpoints.json"
    auth_key = b"u" * 256
    session_string = encode_session_string(
        Storage.SESSION_STRING_FORMAT,
        5,
        12345,
        False,
        auth_key,
        987654321,
        False,
    )

    def connection_factory(**kwargs):
        return FailingConnection(attempts, {}, **kwargs)

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        if server_address == DC5_PRIMARY:
            return False, 0.250, TimeoutError("timed out")

        return True, 0.020, None

    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: cache_file)
    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    client = Client(
        "dc5-cache-update",
        session_string=session_string,
        workdir=tmp_path,
        connection_factory=connection_factory,
    )

    try:
        assert await client.connect() is True
        cache = json.loads(cache_file.read_text())
        cached = cache["endpoints"]["prod:v4:dc5:api"]
        assert cached["server_address"] == DC5_FALLBACK
        assert cached["port"] == 443
    finally:
        if client.is_connected:
            await client.disconnect()


@pytest.mark.asyncio
async def test_get_dc_option_prefers_fastest_reachable_same_dc_endpoint(monkeypatch):
    client = make_endpoint_client([
        dc_option(5, DC5_CONFIG_ENDPOINT),
        dc_option(4, "149.154.167.91"),
    ])
    probes = []

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))

        if server_address == DC5_CONFIG_ENDPOINT:
            return False, 0.250, TimeoutError("timed out")

        if server_address == DC5_FALLBACK:
            return True, 0.020, None

        return True, 0.100, None

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    selected = await client.get_dc_option(5)

    assert selected.id == 5
    assert selected.ip_address == DC5_FALLBACK
    assert selected.port == 443
    assert (DC5_CONFIG_ENDPOINT, 443) in probes
    assert (DC5_FALLBACK, 443) in probes
    assert all(endpoint[0] != "149.154.167.91" for endpoint in probes)


@pytest.mark.asyncio
async def test_set_dc_keeps_current_reachable_same_dc_endpoint(monkeypatch, tmp_path: Path):
    client = make_endpoint_client([dc_option(5, DC5_CONFIG_ENDPOINT)])

    async def fake_probe(server_address, port, timeout):
        if server_address == DC5_CONFIG_ENDPOINT:
            return True, 0.010, None

        return True, 0.020, None

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)
    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: tmp_path / "dc-cache.json")

    await client.set_dc(dc_id=5)

    assert await client.storage.dc_id() == 5
    assert await client.storage.server_address() == DC5_FALLBACK
    assert await client.storage.port() == 443
    assert client.session.server_address == DC5_FALLBACK
    assert client.session.port == 443
    assert client.session.restart_calls == 0


@pytest.mark.asyncio
async def test_set_dc_preserves_explicit_custom_endpoint(monkeypatch, tmp_path: Path):
    custom_address = "203.0.113.10"
    client = make_endpoint_client([dc_option(5, DC5_CONFIG_ENDPOINT)])

    async def fail_probe(*args, **kwargs):
        raise AssertionError("explicit set_dc endpoint must not use endpoint probes")

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fail_probe)
    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: tmp_path / "dc-cache.json")

    await client.set_dc(dc_id=5, server_address=custom_address, port=443)

    assert await client.storage.server_address() == custom_address
    assert await client.storage.port() == 443
    assert client.session.server_address == custom_address
    assert client.session.port == 443
    assert client.session.restart_calls == 1
    assert not (tmp_path / "dc-cache.json").exists()


@pytest.mark.asyncio
async def test_set_dc_preserves_explicit_custom_address_without_port(monkeypatch, tmp_path: Path):
    custom_address = "203.0.113.10"
    client = make_endpoint_client([dc_option(5, DC5_CONFIG_ENDPOINT)])

    async def fail_probe(*args, **kwargs):
        raise AssertionError("explicit set_dc address must not use endpoint probes")

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fail_probe)
    monkeypatch.setattr("pyrogram.dc_options.endpoint_cache_path", lambda: tmp_path / "dc-cache.json")

    await client.set_dc(dc_id=5, server_address=custom_address)

    assert await client.storage.server_address() == custom_address
    assert await client.storage.port() == 443
    assert client.session.server_address == custom_address
    assert client.session.port == 443
    assert client.session.restart_calls == 1
    assert not (tmp_path / "dc-cache.json").exists()


class FakeStorage:
    async def api_id(self):
        return 12345


class MutableStorage:
    def __init__(self, dc_id=5, server_address=DC5_FALLBACK, port=443, test_mode=False):
        self.values = {
            "dc_id": dc_id,
            "server_address": server_address,
            "port": port,
            "test_mode": test_mode,
        }

    async def dc_id(self, value=object):
        if value is not object:
            self.values["dc_id"] = value

        return self.values["dc_id"]

    async def server_address(self, value=object):
        if value is not object:
            self.values["server_address"] = value

        return self.values["server_address"]

    async def port(self, value=object):
        if value is not object:
            self.values["port"] = value

        return self.values["port"]

    async def test_mode(self):
        return self.values["test_mode"]


class LoadedStorage(MutableStorage):
    async def api_id(self):
        return 12345

    async def auth_key(self):
        return b"l" * 256

    async def user_id(self):
        return 987654321

    async def is_bot(self):
        return False


class RunningSession:
    def __init__(self, server_address=DC5_FALLBACK, port=443):
        self.server_address = server_address
        self.port = port
        self.fallback_endpoints = ((server_address, port),)
        self.restart_calls = 0

    async def restart(self):
        self.restart_calls += 1
        self.server_address, self.port = self.fallback_endpoints[0]


def dc_option(dc_id, ip_address, port=443, **flags):
    return raw.types.DcOption(
        id=dc_id,
        ip_address=ip_address,
        port=port,
        ipv6=flags.get("ipv6", False),
        media_only=flags.get("media_only", False),
        cdn=flags.get("cdn", False),
        static=flags.get("static", False),
        tcpo_only=flags.get("tcpo_only", False),
        this_port_only=flags.get("this_port_only", False),
        secret=flags.get("secret"),
    )


def make_endpoint_client(options, *, server_address=DC5_FALLBACK, port=443, proxy=None):
    client = Client.__new__(Client)
    config = SimpleNamespace(this_dc=5, dc_options=options)
    client._Client__config = config
    client.is_connected = True
    client.ipv6 = False
    client.proxy = proxy
    client.storage = MutableStorage(server_address=server_address, port=port)
    client.session = RunningSession(server_address=server_address, port=port)

    async def invoke(query):
        return config

    client.invoke = invoke

    return client


def make_loaded_client(loop, attempts, failures, *, ipv6=False, proxy=None):
    def connection_factory(**kwargs):
        return FailingConnection(attempts, failures, **kwargs)

    client = Client.__new__(Client)
    client.app_version = "test"
    client.business_connections = {}
    client.connect_handler = None
    client.connection_factory = connection_factory
    client.device_model = "test"
    client.disconnect_handler = None
    client.init_connection_params = None
    client.ipv6 = ipv6
    client.lang_code = "en"
    client.lang_pack = ""
    client.loop = loop
    client.media_sessions = {}
    client.name = "loaded-client"
    client.protocol_factory = None
    client.proxy = proxy
    client.session = None
    client.sessions = {}
    client.storage = LoadedStorage()
    client.system_lang_code = "en"
    client.system_version = "test"

    return client


@pytest.mark.asyncio
async def test_get_dc_option_does_not_direct_probe_when_proxy_is_configured(monkeypatch):
    client = make_endpoint_client(
        [dc_option(5, DC5_CONFIG_ENDPOINT)],
        proxy={"hostname": "127.0.0.1", "port": 1080},
    )

    async def fail_probe(*args, **kwargs):
        raise AssertionError("direct TCP endpoint probes must not run when a proxy is configured")

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fail_probe)

    selected = await client.get_dc_option(5)

    assert selected.ip_address == DC5_FALLBACK
    assert selected.port == 443


@pytest.mark.asyncio
async def test_get_dc_option_does_not_apply_prod_fallback_to_media(monkeypatch):
    media_address = "149.154.171.10"
    client = make_endpoint_client([dc_option(5, media_address, media_only=True)])

    async def fail_probe(*args, **kwargs):
        raise AssertionError("single media option should not require prod fallback probing")

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fail_probe)

    selected = await client.get_dc_option(5, is_media=True)

    assert selected.ip_address == media_address
    assert selected.media_only


@pytest.mark.asyncio
async def test_get_dc_option_does_not_apply_prod_fallback_to_test_mode(monkeypatch):
    client = make_endpoint_client([dc_option(5, DC5_CONFIG_ENDPOINT)])
    client.storage.values["test_mode"] = True

    async def fail_probe(*args, **kwargs):
        raise AssertionError("test-mode endpoint selection must not use prod fallback probes")

    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fail_probe)

    selected = await client.get_dc_option(5)

    assert selected.ip_address == DC5_CONFIG_ENDPOINT


@pytest.mark.asyncio
async def test_get_session_does_not_apply_ipv4_prod_fallback_to_ipv6(monkeypatch):
    attempts = []
    probes = []
    ipv6_address = "2001:b28:f23d:f001::a"
    client = make_loaded_client(
        asyncio.get_running_loop(),
        attempts,
        {},
        ipv6=True,
    )

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))
        return True, 0.020, None

    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    session = await client.get_session(
        dc_id=5,
        server_address=ipv6_address,
        port=443,
        temporary=True,
        export_authorization=False,
        order_fallback_endpoints=True,
    )

    try:
        assert attempts == [(ipv6_address, 443)]
        assert probes == []
        assert session.server_address == ipv6_address
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_get_session_preserves_explicit_custom_endpoint(monkeypatch):
    attempts = []
    probes = []
    custom_address = "203.0.113.10"
    client = make_loaded_client(asyncio.get_running_loop(), attempts, {})

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))
        return True, 0.010, None

    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    session = await client.get_session(
        dc_id=5,
        server_address=custom_address,
        port=443,
        temporary=True,
        export_authorization=False,
    )

    try:
        assert attempts == [(custom_address, 443)]
        assert probes == []
        assert session.server_address == custom_address
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_get_session_preserves_explicit_custom_address_without_port(monkeypatch):
    attempts = []
    probes = []
    custom_address = "203.0.113.10"
    client = make_loaded_client(asyncio.get_running_loop(), attempts, {})

    async def fake_send(self, query, *args, **kwargs):
        return object()

    async def fake_recv_worker(self):
        return None

    async def fake_ping_worker(self):
        await self.ping_task_event.wait()

    async def fake_probe(server_address, port, timeout):
        probes.append((server_address, port))
        return True, 0.010, None

    monkeypatch.setattr(Session, "send", fake_send)
    monkeypatch.setattr(Session, "recv_worker", fake_recv_worker)
    monkeypatch.setattr(Session, "ping_worker", fake_ping_worker)
    monkeypatch.setattr("pyrogram.dc_options.probe_tcp_endpoint", fake_probe)

    session = await client.get_session(
        dc_id=5,
        server_address=custom_address,
        temporary=True,
        export_authorization=False,
    )

    try:
        assert attempts == [(custom_address, 443)]
        assert probes == []
        assert session.server_address == custom_address
    finally:
        await session.stop()


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

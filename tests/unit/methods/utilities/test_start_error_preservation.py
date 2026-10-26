"""Exercise real Client.start and native cleanup error with all wire calls mocked."""

import asyncio
import traceback

import pytest

from pyrogram.errors import SessionRevoked
from pyrogram.session.session import SessionCleanupError


@pytest.mark.parametrize("error_type", [ConnectionError, TimeoutError, ValueError, SessionRevoked])
async def test_real_client_preserves_primary_failure_and_chains_cleanup(
    offline_client, monkeypatch, error_type
):
    client = offline_client
    original_disconnect = client.disconnect
    primary = error_type()
    secondary = SessionCleanupError([("recv", RuntimeError("offline failure"))])
    disconnects = []

    async def failing_invoke(_query):
        raise primary

    async def failing_disconnect():
        disconnects.append(True)
        raise secondary

    monkeypatch.setattr(client, "invoke", failing_invoke)
    monkeypatch.setattr(client, "disconnect", failing_disconnect)
    try:
        with pytest.raises(error_type) as caught:
            await client.start()
        assert caught.value is primary
        assert primary.__cause__ is secondary
        assert disconnects == [True]
        assert not client.is_initialized
        formatted = "".join(
            traceback.format_exception(type(primary), primary, primary.__traceback__)
        )
        assert "SessionCleanupError" in formatted
        assert error_type.__name__ in formatted
        assert len(formatted) < 20_000  # Cyclic context/cause references render finitely.
    finally:
        await original_disconnect()


async def test_real_client_cleanup_cancellation_remains_cancellation(offline_client, monkeypatch):
    client = offline_client
    original_disconnect = client.disconnect

    async def failing_invoke(_query):
        raise ConnectionError("offline failure")

    async def canceled_disconnect():
        raise asyncio.CancelledError

    monkeypatch.setattr(client, "invoke", failing_invoke)
    monkeypatch.setattr(client, "disconnect", canceled_disconnect)
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.start()
    finally:
        await original_disconnect()


async def test_real_client_start_cancellation_disconnects_before_return(
    offline_client, monkeypatch
):
    client = offline_client
    original_disconnect = client.disconnect
    disconnects = []

    async def canceled_invoke(_query):
        raise asyncio.CancelledError

    async def record_disconnect():
        disconnects.append(True)
        await original_disconnect()

    monkeypatch.setattr(client, "invoke", canceled_invoke)
    monkeypatch.setattr(client, "disconnect", record_disconnect)
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.start()
        assert disconnects == [True]
        assert not client.is_connected and not client.is_initialized
    finally:
        await original_disconnect()


async def test_keyboard_interrupt_remains_primary_after_failed_cleanup(offline_client, monkeypatch):
    client = offline_client
    original_disconnect = client.disconnect
    primary = KeyboardInterrupt()
    secondary = SessionCleanupError([("recv", RuntimeError())])

    async def interrupted_invoke(_query):
        raise primary

    async def failed_disconnect():
        raise secondary

    monkeypatch.setattr(client, "invoke", interrupted_invoke)
    monkeypatch.setattr(client, "disconnect", failed_disconnect)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            await client.start()
        assert caught.value is primary
        assert primary.__cause__ is secondary
    finally:
        await original_disconnect()

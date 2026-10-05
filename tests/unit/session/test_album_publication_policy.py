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

import pytest

from pyrogram import raw
from pyrogram.errors import FloodWait
from pyrogram.session.session import SessionNotReady, single_attempt_album_publication
from tests.unit.session.test_session import _session, _started_session


def album_query():
    return raw.functions.messages.SendMultiMedia(peer=raw.types.InputPeerSelf(), multi_media=[])


@pytest.mark.asyncio
async def test_readiness_signal_is_invocation_local_and_has_no_wire(monkeypatch):
    session = _session()
    monkeypatch.setattr(session, "WAIT_TIMEOUT", 0.001)
    with pytest.raises(SessionNotReady) as caught:
        await session.invoke(album_query())
    assert isinstance(caught.value, TimeoutError)
    assert caught.value.query_name == "messages.SendMultiMedia"
    assert caught.value.no_send is True
    assert caught.value.session is session
    assert session.connection is None
    assert session.results == {}


@pytest.mark.asyncio
async def test_policy_stops_after_one_real_transport_write_and_resets(monkeypatch):
    session = _started_session()
    session.client.session = session
    session.client.is_connected = True
    monkeypatch.setattr(session, "WAIT_TIMEOUT", 0.001)
    wire = session.connection.sent
    with single_attempt_album_publication(session.client) as policy:
        with pytest.raises(TimeoutError) as caught:
            await session.invoke(album_query(), timeout=0.001, retry_delay=0)
        assert not isinstance(caught.value, SessionNotReady)
        assert len(wire) == 1
        assert policy.attempted is True
    with pytest.raises(TimeoutError):
        await session.invoke(album_query(), timeout=0.001, retry_delay=0)
    assert len(wire) == 11  # Default remains ten attempts outside the scope.


@pytest.mark.asyncio
async def test_policy_denies_inherited_child_and_other_session_and_other_query(monkeypatch):
    session = _started_session()
    session.client.session = session
    other = _started_session()
    monkeypatch.setattr(session, "WAIT_TIMEOUT", 0.001)
    monkeypatch.setattr(other, "WAIT_TIMEOUT", 0.001)

    async def unknown(target, query):
        with pytest.raises(TimeoutError):
            await target.invoke(query, timeout=0.001, retry_delay=0)

    with single_attempt_album_publication(session.client) as policy:
        await asyncio.create_task(unknown(session, album_query()))
        await unknown(other, album_query())
        await unknown(session, raw.functions.help.GetConfig())
        assert policy.attempted is False
        await unknown(session, album_query())
    assert len(session.connection.sent) == 21
    assert len(other.connection.sent) == 10


@pytest.mark.asyncio
async def test_policy_propagates_known_flood_rejection_without_native_wait(monkeypatch):
    session = _started_session()
    session.client.session = session
    calls = []

    async def rejection(*args, **kwargs):
        calls.append(args)
        raise FloodWait(1)

    monkeypatch.setattr(session, "send", rejection)
    with single_attempt_album_publication(session.client):
        with pytest.raises(FloodWait):
            await session.invoke(album_query(), sleep_threshold=60)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_tasks_on_one_session_keep_independent_attempt_policy():
    session = _started_session()
    session.client.session = session

    async def scoped():
        with single_attempt_album_publication(session.client) as policy:
            with pytest.raises(TimeoutError):
                await session.invoke(album_query(), timeout=0.001, retry_delay=0)
            assert policy.attempted

    async def ordinary():
        with pytest.raises(TimeoutError):
            await session.invoke(album_query(), timeout=0.001, retry_delay=0)

    await asyncio.gather(scoped(), ordinary())
    assert len(session.connection.sent) == 11


@pytest.mark.asyncio
async def test_nested_native_wrappers_still_scope_exact_album():
    session = _started_session()
    session.client.session = session
    query = raw.functions.InvokeWithTakeout(
        takeout_id=1, query=raw.functions.InvokeWithoutUpdates(query=album_query())
    )
    with single_attempt_album_publication(session.client) as policy:
        with pytest.raises(TimeoutError):
            await session.invoke(query, timeout=0.001, retry_delay=0)
        assert policy.attempted
    assert len(session.connection.sent) == 1


@pytest.mark.asyncio
async def test_same_owner_replacement_session_fails_closed_before_wire():
    session = _started_session()
    session.client.session = session
    replacement = _started_session()
    replacement.client = session.client
    with single_attempt_album_publication(session.client):
        session.client.session = replacement
        with pytest.raises(RuntimeError, match="album_publication_session_changed"):
            await replacement.invoke(album_query(), timeout=0.001, retry_delay=0)
    assert not replacement.connection.sent
    assert not session.connection.sent


@pytest.mark.asyncio
async def test_cancellation_unwinds_policy_and_default_is_restored():
    session = _started_session()
    session.client.session = session
    with pytest.raises(asyncio.CancelledError):
        with single_attempt_album_publication(session.client):
            raise asyncio.CancelledError
    with pytest.raises(TimeoutError):
        await session.invoke(album_query(), timeout=0.001, retry_delay=0)
    assert len(session.connection.sent) == 10

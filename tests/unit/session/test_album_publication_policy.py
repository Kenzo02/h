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
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pyrogram import raw
from pyrogram.errors import FloodWait
from pyrogram.session.session import (
    PublicationUnconfirmed,
    SessionNotReady,
    ordinary_publication,
    single_attempt_album_publication,
)
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


def ordinary_query(method="SendMessage"):
    peer = raw.types.InputPeerChannel(channel_id=67890, access_hash=1)
    if method == "SendMultiMedia":
        return raw.functions.messages.SendMultiMedia(
            peer=peer,
            multi_media=[
                raw.types.InputSingleMedia(
                    media=raw.types.InputMediaEmpty(), random_id=random_id, message="Fixture"
                )
                for random_id in (71, 72)
            ],
        )
    if method == "SendMedia":
        return raw.functions.messages.SendMedia(
            peer=peer, media=raw.types.InputMediaEmpty(), random_id=71, message="Fixture"
        )
    return raw.functions.messages.SendMessage(peer=peer, random_id=71, message="Fixture")


def ordinary_updates(ids=(71,)):
    # Destination order is deliberately different from source/random-ID order.
    updates = []
    for index, random_id in enumerate(ids):
        target = 902 - index
        updates.append(raw.types.UpdateMessageID(random_id=random_id, id=target))
        updates.append(
            raw.types.UpdateNewChannelMessage(
                message=raw.types.Message(
                    id=target,
                    peer_id=raw.types.PeerChannel(channel_id=67890),
                    date=1,
                    message="Fixture",
                    out=True,
                ),
                pts=index + 1,
                pts_count=1,
            )
        )
    return raw.types.Updates(updates=list(reversed(updates)), users=[], chats=[], date=1, seq=1)


def ordinary_transport(monkeypatch, outcomes):
    session = _started_session()
    session.client.session = session
    session.client.me = SimpleNamespace(id=999, is_bot=False)
    session.client.is_connected = True
    session.WAIT_TIMEOUT = 0.001
    queries, publications, intents, receipts, waits = [], set(), [], [], []
    factory = session.msg_factory.create

    async def create(query):
        queries.append(query)
        return await factory(query)

    monkeypatch.setattr(session.msg_factory, "create", create)

    async def transport(payload):
        session.connection.sent.append(payload)
        query = queries[-1]
        inner = query
        while hasattr(inner, "query"):
            inner = inner.query
        ids = (
            tuple(item.random_id for item in inner.multi_media)
            if hasattr(inner, "multi_media")
            else (inner.random_id,)
        )
        publications.add((inner.peer.write(), ids))
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        result = outcome if outcome is not None else ordinary_updates(ids)
        pending = next(iter(session.results.values()))
        pending.value = result
        pending.event.set()

    monkeypatch.setattr(session.connection, "send", transport)

    async def wait(attempt, phase):
        waits.append((attempt, phase))

    hooks = {
        "account_id": 999,
        "peer": ordinary_query().peer,
        "on_intent": lambda *data: intents.append(data),
        "on_receipt": receipts.append,
        "before_attempt": AsyncMock(),
        "wait_retry": wait,
    }
    return SimpleNamespace(
        session=session,
        queries=queries,
        publications=publications,
        intents=intents,
        receipts=receipts,
        waits=waits,
        hooks=hooks,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["SendMessage", "SendMedia", "SendMultiMedia"])
@pytest.mark.parametrize("first", ["lost", "duplicate400", "duplicate500"])
async def test_exact_query_recovery_uses_native_errors_and_one_provider_publication(
    monkeypatch, method, first
):
    error = (
        TimeoutError("lost")
        if first == "lost"
        else raw.types.RpcError(
            error_code=400 if first == "duplicate400" else 500, error_message="RANDOM_ID_DUPLICATE"
        )
    )
    f = ordinary_transport(monkeypatch, [error, None])
    query = ordinary_query(method)
    wrapped = raw.functions.InvokeWithTakeout(
        takeout_id=1, query=raw.functions.InvokeWithoutUpdates(query=query)
    )
    with ordinary_publication(
        f.session.client,
        method=method,
        source_ids=(101, 102) if method == "SendMultiMedia" else (101,),
        **f.hooks,
    ) as policy:
        await f.session.invoke(wrapped, timeout=0.001, retry_delay=0)
    assert len(f.session.connection.sent) == 2 and len(f.publications) == 1
    assert f.queries[0] is f.queries[1] is wrapped
    assert f.queries[0].write() == f.intents[0][0] == policy.query_bytes
    assert len(f.intents) == 1 and len(f.waits) == 1
    assert f.receipts == [(902, 901) if method == "SendMultiMedia" else (902,)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError(),
        raw.types.RpcError(error_code=400, error_message="RANDOM_ID_DUPLICATE"),
        raw.types.RpcError(error_code=500, error_message="RANDOM_ID_DUPLICATE"),
    ],
)
async def test_native_uncertainty_exhaustion_has_sticky_fence(monkeypatch, outcome):
    f = ordinary_transport(monkeypatch, [outcome])
    with ordinary_publication(
        f.session.client, method="SendMessage", source_ids=(101,), **f.hooks
    ) as policy:
        with pytest.raises(PublicationUnconfirmed):
            await f.session.invoke(ordinary_query(), timeout=0.001)
        with pytest.raises(PublicationUnconfirmed, match="fresh_query"):
            await f.session.invoke(ordinary_query(), timeout=0.001)
        assert policy.unknown
    assert len(f.session.connection.sent) == 4 and len(f.publications) == 1
    assert len(f.waits) == 3 and not f.receipts


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["partial", "foreign", "duplicate", "conflict", "peer"])
async def test_native_receipt_requires_exact_query_mapping(monkeypatch, bad):
    result = ordinary_updates((71, 72))
    if bad == "partial":
        result.updates.pop()
    elif bad == "foreign":
        result.updates.append(raw.types.UpdateMessageID(id=901, random_id=999))
    elif bad == "duplicate":
        result.updates.append(raw.types.UpdateMessageID(id=902, random_id=71))
    elif bad == "conflict":
        next(u for u in result.updates if isinstance(u, raw.types.UpdateMessageID)).id = 902
    else:
        next(u for u in result.updates if hasattr(u, "message")).message.peer_id.channel_id = 999
    f = ordinary_transport(monkeypatch, [result])
    with ordinary_publication(
        f.session.client, method="SendMultiMedia", source_ids=(101, 102), **f.hooks
    ):
        with pytest.raises(PublicationUnconfirmed, match="receipt_unconfirmed"):
            await f.session.invoke(ordinary_query("SendMultiMedia"))
    assert not f.receipts and len(f.session.connection.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cancel", "account", "session", "mutate", "reject"])
async def test_native_retry_cannot_escape_identity_or_earlier_unknown(monkeypatch, change):
    f = ordinary_transport(
        monkeypatch,
        [
            TimeoutError(),
            raw.types.RpcError(error_code=400, error_message="FILE_REFERENCE_EXPIRED"),
        ],
    )
    query = ordinary_query()

    async def wait(*args):
        if change == "cancel":
            raise asyncio.CancelledError
        if change == "account":
            f.session.client.me.id = 1000
        elif change == "session":
            f.session.client.session = _started_session()
        elif change == "mutate":
            query.message = "Changed caption"

    f.hooks["wait_retry"] = wait
    with ordinary_publication(f.session.client, method="SendMessage", source_ids=(101,), **f.hooks):
        with pytest.raises(
            asyncio.CancelledError if change == "cancel" else PublicationUnconfirmed
        ):
            await f.session.invoke(query)
    assert len(f.session.connection.sent) == (2 if change == "reject" else 1)
    assert not f.receipts


@pytest.mark.asyncio
async def test_native_short_sent_receipt_and_pre_enrichment_failure(monkeypatch):
    result = raw.types.UpdateShortSentMessage(id=902, pts=1, pts_count=1, date=1, out=True)
    f = ordinary_transport(monkeypatch, [result])
    f.session.client.fetch_peers = AsyncMock(side_effect=FloodWait(1))
    with ordinary_publication(f.session.client, method="SendMessage", source_ids=(101,), **f.hooks):
        with pytest.raises(FloodWait):
            await f.session.client.invoke(ordinary_query())
    assert f.receipts == [(902,)] and len(f.session.connection.sent) == 1


@pytest.mark.asyncio
async def test_ordinary_policy_inherited_child_other_client_and_unrelated_query_keep_defaults(
    monkeypatch,
):
    f = ordinary_transport(monkeypatch, [TimeoutError()])
    other = ordinary_transport(monkeypatch, [TimeoutError()])

    async def default(session):
        with pytest.raises(TimeoutError):
            await session.invoke(ordinary_query(), timeout=0.001, retry_delay=0)

    with ordinary_publication(
        f.session.client, method="SendMessage", source_ids=(101,), **f.hooks
    ) as policy:
        await asyncio.create_task(default(f.session))
        await default(other.session)
        # A preparatory/non-publication invocation does not consume business intent.
        f.session.is_started.clear()
        with pytest.raises(SessionNotReady):
            await f.session.invoke(raw.functions.help.GetConfig())
        assert not policy.attempted and policy.query_bytes is None
    assert not f.intents and not other.intents
    assert len(f.session.connection.sent) == len(other.session.connection.sent) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["peer", "method", "session_id", "bot"])
async def test_ordinary_policy_wrong_binding_fails_before_wire(monkeypatch, change):
    f = ordinary_transport(monkeypatch, [None])
    query = ordinary_query("SendMedia" if change == "method" else "SendMessage")
    with ordinary_publication(f.session.client, method="SendMessage", source_ids=(101,), **f.hooks):
        if change == "peer":
            query.peer.channel_id = 99
        elif change == "session_id":
            f.session.session_id = bytes(8)
        elif change == "bot":
            f.session.client.me.is_bot = True
        with pytest.raises(PublicationUnconfirmed):
            await f.session.invoke(query)
    assert not f.intents and not f.session.connection.sent


@pytest.mark.asyncio
async def test_ordinary_unsupported_publication_envelope_fails_before_wire(monkeypatch):
    f = ordinary_transport(monkeypatch, [None])
    query = raw.functions.InvokeWithBusinessConnection(
        connection_id="fixture", query=ordinary_query()
    )
    with ordinary_publication(f.session.client, method="SendMessage", source_ids=(101,), **f.hooks):
        with pytest.raises(PublicationUnconfirmed, match="unsupported_envelope"):
            await f.session.invoke(query)
    assert not f.intents and not f.session.connection.sent


@pytest.mark.asyncio
async def test_ordinary_missing_loaded_native_contract_fails_before_wire(monkeypatch):
    f = ordinary_transport(monkeypatch, [None])
    monkeypatch.delattr(type(f.session), "ORDINARY_PUBLICATION_CONTRACT_VERSION")
    with pytest.raises(PublicationUnconfirmed, match="native_contract_unavailable"):
        with ordinary_publication(
            f.session.client, method="SendMessage", source_ids=(101,), **f.hooks
        ):
            await f.session.invoke(ordinary_query())
    assert not f.intents and not f.session.connection.sent

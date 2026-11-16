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

"""Public update ingress must not hydrate the same known peers for every post."""

from __future__ import annotations as _annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pyrogram import Client, raw, types
from pyrogram.storage import UpdateState


def user(peer_id=201, *, minimal=False, **kwargs):
    return raw.types.User(id=peer_id, min=minimal, access_hash=22, first_name="Fixture", **kwargs)


def channel(*, minimal=False, **kwargs):
    return raw.types.Channel(
        id=101,
        min=minimal,
        access_hash=33,
        title="Fixture",
        date=1,
        photo=raw.types.ChatPhotoEmpty(),
        megagroup=True,
        **kwargs,
    )


def packet(message_id, *, peer_id=201, users=None, chats=None):
    update = raw.types.UpdateNewChannelMessage(
        message=raw.types.Message(
            id=message_id,
            peer_id=raw.types.PeerChannel(channel_id=101),
            from_id=raw.types.PeerUser(user_id=peer_id),
            date=1,
            message="fixture",
        ),
        pts=1000 + message_id,
        pts_count=1,
    )
    return raw.types.Updates(
        updates=[update],
        users=users if users is not None else [user(peer_id, minimal=True)],
        chats=chats if chats is not None else [channel(minimal=True)],
        date=1,
        seq=message_id,
    )


@pytest.fixture
async def client(monkeypatch):
    app = Client("update_peer_cache_probe", in_memory=True)
    await app.storage.open()
    app.is_connected = True
    app.case_calls = []
    app.case_deliveries = []
    app.case_users = {201: user()}

    async def invoke(query, **kwargs):
        assert isinstance(query, raw.functions.updates.GetChannelDifference)
        app.case_calls.append(query)
        return raw.types.updates.ChannelDifference(
            pts=query.pts + 1,
            final=True,
            new_messages=[],
            other_updates=[],
            users=list(app.case_users.values()),
            chats=[channel()],
        )

    app.session = SimpleNamespace(invoke=invoke)
    monkeypatch.setattr(
        app.dispatcher,
        "enqueue",
        lambda update, *, users, chats: app.case_deliveries.append((update, users, chats)),
    )
    await app.fetch_peers([channel()])
    try:
        yield app
    finally:
        await app.storage.close()


async def test_one_cold_hydration_then_ordered_warm_burst(client):
    await asyncio.gather(*(client.handle_updates(packet(i)) for i in range(1, 41)))
    assert len(client.case_calls) == 1
    assert [row[0].message.id for row in client.case_deliveries] == list(range(1, 41))
    states = await client.storage.get_update_states(-1000000000101)
    assert states[0].pts == 1040
    assert not client._update_state_lock.locked()


async def test_full_peers_fetched_elsewhere_avoid_update_rpc(client):
    await client.fetch_peers([user()])
    incoming = packet(1)
    await client.handle_updates(incoming)
    assert client.case_calls == []
    assert len(client.case_deliveries) == 1
    assert client.case_deliveries[0][0] is incoming.updates[0]


async def test_one_cold_hydration_for_the_remaining_same_peer_batch(client):
    incoming = packet(1)
    incoming.updates = [packet(i).updates[0] for i in range(1, 41)]
    await client.handle_updates(incoming)
    assert len(client.case_calls) == 1
    assert [row[0] for row in client.case_deliveries] == incoming.updates
    states = await client.storage.get_update_states(-1000000000101)
    assert states[0].pts == 1040


async def test_user_and_channel_ids_have_separate_cache_namespaces(client):
    await client.fetch_peers([user(101), channel()])
    await client.handle_updates(packet(1, peer_id=101))
    assert client.case_calls == []
    assert isinstance(client.case_deliveries[0][1][101], raw.types.User)
    assert isinstance(client.case_deliveries[0][2][101], raw.types.Channel)


async def test_min_user_keeps_full_identity_contact_and_hash(client):
    full = user(contact=True, mutual_contact=True, username="fixture_full", phone="")
    full.first_name = "Full fixture"
    full.status = raw.types.UserStatusOffline(was_online=1)
    full.photo = raw.types.UserProfilePhoto(photo_id=8, dc_id=2)
    await client.fetch_peers([full])
    minimal = user(minimal=True)
    minimal.first_name = "Minimal fixture"
    minimal.access_hash = 999
    minimal.status = raw.types.UserStatusOnline(expires=9)
    minimal.photo = raw.types.UserProfilePhotoEmpty()
    await client.handle_updates(packet(1, users=[minimal]))
    assert client.case_calls == []
    merged = client.case_deliveries[0][1][201]
    assert merged is not full and merged is not minimal
    assert merged.first_name == "Full fixture"
    assert merged.contact and merged.mutual_contact
    assert merged.username == "fixture_full" and merged.phone == ""
    assert merged.access_hash == 22 and not merged.min
    assert merged.status is full.status and merged.photo is full.photo
    assert minimal.min and minimal.access_hash == 999
    assert (await client.storage.get_peer_by_id(201)).access_hash == 22


async def test_min_user_applies_permitted_photo_status_and_full_hash(client):
    full = user()
    full.status = raw.types.UserStatusEmpty()
    await client.fetch_peers([full])
    minimal = user(minimal=True, apply_min_photo=True, phone="")
    minimal.photo = raw.types.UserProfilePhoto(photo_id=9, dc_id=2)
    minimal.status = raw.types.UserStatusOnline(expires=9)
    minimal.access_hash = 44
    await client.handle_updates(packet(1, users=[minimal]))
    assert client.case_calls == []
    merged = client.case_deliveries[0][1][201]
    assert merged.photo is minimal.photo and merged.status is minimal.status
    assert merged.access_hash == 44
    assert full.access_hash == 22


async def test_min_channel_merges_only_allowed_fields(client):
    full = channel(creator=True, forum=False)
    await client.fetch_peers([full, user()])
    minimal = channel(minimal=True, forum=True, noforwards=True)
    minimal.title = "Changed fixture"
    minimal.access_hash = 999
    await client.handle_updates(packet(1, chats=[minimal]))
    assert client.case_calls == []
    merged = client.case_deliveries[0][2][101]
    assert merged.creator and merged.access_hash == 33 and not merged.min
    assert merged.forum and merged.noforwards and merged.title == "Changed fixture"
    assert full.title == "Fixture" and not full.forum
    assert minimal.min and minimal.access_hash == 999


async def test_unknown_min_peer_still_hydrates(client):
    await client.fetch_peers([user()])
    client.case_users[202] = user(202)
    await client.handle_updates(packet(1, peer_id=202))
    assert len(client.case_calls) == 1
    assert not client.case_deliveries[0][1][202].min


async def test_new_full_snapshot_replaces_cached_metadata(client):
    await client.fetch_peers([user()])
    replacement = user()
    replacement.first_name = "Replacement fixture"
    await client.fetch_peers([replacement])
    await client.handle_updates(packet(1))
    assert client.case_calls == []
    assert client.case_deliveries[0][1][201].first_name == "Replacement fixture"


async def test_empty_user_invalidates_cached_full_snapshot(client):
    await client.fetch_peers([user()])
    await client.fetch_peers([raw.types.UserEmpty(id=201)])
    await client.handle_updates(packet(1))
    assert len(client.case_calls) == 1


async def test_forbidden_channel_invalidates_cached_full_snapshot(client):
    await client.fetch_peers([user()])
    await client.fetch_peers([raw.types.ChannelForbidden(id=101, access_hash=33, title="Fixture")])
    await client.handle_updates(packet(1))
    assert len(client.case_calls) == 1


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        raw.types.UpdateUserName(user_id=201, first_name="Updated", last_name="", usernames=[]),
        raw.types.UpdateUserPhone(user_id=201, phone=""),
        raw.types.UpdateUserStatus(user_id=201, status=raw.types.UserStatusEmpty()),
        raw.types.UpdateChannel(channel_id=101),
    ],
    ids=["name", "phone", "status", "channel"],
)
async def test_metadata_updates_invalidate_full_snapshots(client, changed, short):
    await client.fetch_peers([user()])
    if short:
        await client.handle_updates(raw.types.UpdateShort(update=changed, date=1))
        await client.handle_updates(packet(1))
    else:
        incoming = packet(1)
        incoming.updates.insert(0, changed)
        await client.handle_updates(incoming)
    assert len(client.case_calls) == 1
    assert len(client.case_deliveries) == 2
    assert client.case_deliveries[0][0] is changed


async def test_peer_cache_is_bounded_and_eviction_keeps_hydration(client):
    await client.fetch_peers([user()])
    await client.fetch_peers([user(i) for i in range(10000, 14096)])
    await client.fetch_peers([channel()])
    await client.handle_updates(packet(1, peer_id=14095))
    assert client.case_calls == []
    await client.handle_updates(packet(2))
    assert len(client.case_calls) == 1
    assert [row[0].message.id for row in client.case_deliveries] == [1, 2]


async def test_cache_is_scoped_to_client(client):
    await client.fetch_peers([user()])
    other = Client("separate_peer_cache_probe", in_memory=True)
    await other.storage.open()
    other.is_connected = True
    other.session = client.session
    other.dispatcher.enqueue = lambda update, **kwargs: None
    try:
        await other.fetch_peers([channel()])
        await other.handle_updates(packet(1))
        assert len(client.case_calls) == 1
    finally:
        await other.storage.close()


async def test_hydration_failure_is_safe_and_keeps_the_live_update(client, caplog):
    async def fail(query, **kwargs):
        raise TimeoutError("private fixture detail")

    client.session.invoke = fail
    await client.handle_updates(packet(1))
    assert len(client.case_deliveries) == 1
    assert "update_peer_hydration_failed" in caplog.text
    assert "channel_id=101" in caplog.text and "message_id=1" in caplog.text
    assert "error_type=TimeoutError" in caplog.text
    assert "private fixture detail" not in caplog.text


@pytest.mark.parametrize(
    "route", ["short-message", "short-chat", "recover-global", "recover-channel"]
)
@pytest.mark.parametrize("field", ["first_name", "username", "phone", "status"])
async def test_difference_metadata_invalidates_through_public_ingress(client, route, field):
    before = user()
    before.status = raw.types.UserStatusOffline(was_online=1)
    await client.fetch_peers([before])
    after = user()
    after.first_name = "Updated fixture"
    after.username = "fixture_updated"
    after.phone = "fixture_phone"
    after.status = raw.types.UserStatusOnline(expires=9)
    client.case_users[201] = after
    changed = (
        raw.types.UpdateUserPhone(user_id=201, phone=after.phone)
        if field == "phone"
        else raw.types.UpdateUserStatus(user_id=201, status=after.status)
        if field == "status"
        else raw.types.UpdateUserName(
            user_id=201, first_name=after.first_name, last_name="", usernames=[]
        )
    )
    native_invoke = client.session.invoke
    first = True

    async def invoke(query, **kwargs):
        nonlocal first
        if not first:
            return await native_invoke(query, **kwargs)
        first = False
        if route == "recover-channel":
            return raw.types.updates.ChannelDifference(
                pts=1002,
                final=True,
                new_messages=[],
                other_updates=[changed],
                users=[],
                chats=[],
            )
        return raw.types.updates.Difference(
            new_messages=[],
            new_encrypted_messages=[],
            other_updates=[changed],
            users=[],
            chats=[],
            state=raw.types.updates.State(pts=1002, qts=0, date=1, seq=1, unread_count=0),
        )

    client.session.invoke = invoke
    if route.startswith("recover"):
        state_id = -1000000000101 if route == "recover-channel" else 0
        await client.storage.set_update_state(UpdateState(state_id, 1000, 0, 1, 1))
        assert await client.recover_gaps(state_id) == (0, 1)
    elif route == "short-message":
        await client.handle_updates(
            raw.types.UpdateShortMessage(
                id=1,
                user_id=201,
                message="fixture",
                pts=1001,
                pts_count=1,
                date=1,
            )
        )
    else:
        await client.handle_updates(
            raw.types.UpdateShortChatMessage(
                id=1,
                from_id=201,
                chat_id=101,
                message="fixture",
                pts=1001,
                pts_count=1,
                date=1,
            )
        )
    await client.handle_updates(packet(2))
    delivered = client.case_deliveries[-1][1][201]
    assert getattr(delivered, field) == getattr(after, field)
    assert len(client.case_calls) == 1


@pytest.mark.parametrize("method", ["get_users", "set_username"])
async def test_full_vector_and_single_peer_responses_refresh_snapshot(client, method):
    await client.fetch_peers([user()])
    replacement = user()
    replacement.first_name = "Fresh full fixture"
    replacement.username = "fixture_updated"
    client.case_users[201] = replacement
    native_invoke = client.session.invoke

    async def invoke(query, **kwargs):
        if isinstance(query, raw.functions.users.GetUsers):
            return types.List([replacement])
        if isinstance(query, raw.functions.account.UpdateUsername):
            return replacement
        return await native_invoke(query, **kwargs)

    client.session.invoke = invoke
    if method == "get_users":
        assert (await client.get_users(201)).first_name == replacement.first_name
    else:
        assert await client.set_username("fixture_updated")
    await client.handle_updates(packet(1))
    assert client.case_deliveries[-1][1][201].first_name == replacement.first_name
    assert client.case_calls == []


async def test_logout_clears_snapshots_even_if_local_stop_fails(client, monkeypatch):
    await client.fetch_peers([user()])
    replacement = user()
    replacement.access_hash = 77
    client.case_users[201] = replacement
    native_invoke = client.session.invoke

    async def invoke(query, **kwargs):
        if isinstance(query, raw.functions.auth.LogOut):
            return raw.types.auth.LoggedOut()
        return await native_invoke(query, **kwargs)

    client.session.invoke = invoke
    monkeypatch.setattr(client, "stop", AsyncMock(side_effect=RuntimeError("fixture stop failure")))
    with pytest.raises(RuntimeError, match="fixture stop failure"):
        await client.log_out()
    await client.handle_updates(packet(1))
    assert client.case_deliveries[-1][1][201].access_hash == 77
    assert (await client.storage.get_peer_by_id(201)).access_hash == 77


@pytest.mark.parametrize("same_auth", [False, True])
async def test_transport_reconnect_keeps_cache_only_for_same_auth(client, same_auth):
    client.session.auth_key = b"fixture-authorization-A"
    await client.fetch_peers([channel(), user()])
    replacement = user()
    replacement.access_hash = 77
    client.case_users[201] = replacement
    client.session = SimpleNamespace(
        invoke=client.session.invoke,
        auth_key=bytes(bytearray(b"fixture-authorization-A"))
        if same_auth
        else b"fixture-authorization-B",
    )
    await client.handle_updates(packet(1))
    assert client.case_deliveries[-1][1][201].access_hash == (22 if same_auth else 77)
    assert len(client.case_calls) == (0 if same_auth else 1)


@pytest.mark.parametrize("qr", [False, True])
async def test_new_authorization_on_same_transport_does_not_reuse_old_peer_hash(client, qr):
    await client.fetch_peers([user()])
    replacement = user()
    replacement.access_hash = 77
    client.case_users[201] = replacement
    native_invoke = client.session.invoke

    async def invoke(query, **kwargs):
        if isinstance(
            query, (raw.functions.auth.ImportAuthorization, raw.functions.auth.ImportLoginToken)
        ):
            authorized = raw.types.auth.Authorization(user=user(202))
            return raw.types.auth.LoginTokenSuccess(authorization=authorized) if qr else authorized
        return await native_invoke(query, **kwargs)

    client.session.invoke = invoke
    await client.invoke(
        raw.functions.auth.ImportLoginToken(token=b"fixture")
        if qr
        else raw.functions.auth.ImportAuthorization(id=1, bytes=b"fixture")
    )
    await client.handle_updates(packet(1))
    assert client.case_deliveries[-1][1][201].access_hash == 77


async def test_peer_snapshot_expiry_is_not_extended_by_minimal_traffic(client, monkeypatch):
    monotonic = time.monotonic
    offset = 0
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + offset)
    await client.fetch_peers([user()])
    offset = 599
    await asyncio.gather(*(client.handle_updates(packet(i)) for i in range(1, 41)))
    assert client.case_calls == []
    replacement = user()
    replacement.first_name = "Expired snapshot refreshed"
    client.case_users[201] = replacement
    offset = 601
    await client.handle_updates(packet(41))
    assert client.case_deliveries[-1][1][201].first_name == replacement.first_name
    await asyncio.gather(*(client.handle_updates(packet(i)) for i in range(42, 82)))
    assert len(client.case_calls) == 1


async def test_fresh_peer_bundled_with_difference_metadata_stays_warm(client):
    await client.fetch_peers([user()])
    replacement = user()
    replacement.first_name = "Bundled fresh fixture"
    changed = raw.types.UpdateUserName(
        user_id=201,
        first_name=replacement.first_name,
        last_name="",
        usernames=[],
    )
    native_invoke = client.session.invoke

    async def invoke(query, **kwargs):
        if isinstance(query, raw.functions.updates.GetDifference):
            return raw.types.updates.Difference(
                new_messages=[],
                new_encrypted_messages=[],
                other_updates=[changed],
                users=[replacement],
                chats=[],
                state=raw.types.updates.State(pts=1002, qts=0, date=1, seq=1, unread_count=0),
            )
        return await native_invoke(query, **kwargs)

    client.session.invoke = invoke
    await client.storage.set_update_state(UpdateState(0, 1000, 0, 1, 1))
    assert await client.recover_gaps(0) == (0, 1)
    await client.handle_updates(packet(1))
    assert client.case_calls == []
    assert client.case_deliveries[-1][1][201].first_name == replacement.first_name


async def test_logout_and_late_old_response_cannot_repopulate_new_authorization(
    client, monkeypatch
):
    client.session.auth_key = b"fixture-authorization-A"
    await client.fetch_peers([user(), channel()])
    native_invoke = client.session.invoke
    started, release = asyncio.Event(), asyncio.Event()

    async def invoke(query, **kwargs):
        if isinstance(query, raw.functions.users.GetUsers):
            started.set()
            await release.wait()
            return [user()]
        if isinstance(query, raw.functions.auth.LogOut):
            return raw.types.auth.LoggedOut()
        return await native_invoke(query, **kwargs)

    client.session.invoke = invoke
    monkeypatch.setattr(client, "stop", AsyncMock())
    monkeypatch.setattr(client.storage, "delete", AsyncMock())
    old_call = asyncio.create_task(client.get_users(201))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await client.log_out()
        client.session = SimpleNamespace(invoke=native_invoke, auth_key=b"fixture-authorization-B")
        replacement = user()
        replacement.access_hash = 77
        await client.fetch_peers([replacement, channel()])
        release.set()
        await old_call
        # The old request still returns to its caller, but cannot overwrite
        # either the new authorization's persistent peer or its snapshot.
        assert (await client.storage.get_peer_by_id(201)).access_hash == 77
        await client.handle_updates(packet(1))
        assert client.case_calls == []
        assert client.case_deliveries[-1][1][201].access_hash == 77
    finally:
        release.set()
        await asyncio.gather(old_call, return_exceptions=True)

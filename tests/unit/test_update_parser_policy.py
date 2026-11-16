"""An opt-in raw-only admission must retain protocol and raw delivery."""

from __future__ import annotations as _annotations

from types import SimpleNamespace

import pytest

from pyrogram import Client, raw, filters
from pyrogram.handlers import MessageHandler, RawUpdateHandler


def user(i, minimal=False):
    return raw.types.User(id=i, min=minimal, access_hash=22, first_name="Fixture")


def channel(i):
    return raw.types.Channel(
        id=i,
        access_hash=33,
        title="Fixture",
        date=1,
        photo=raw.types.ChatPhotoEmpty(),
        megagroup=True,
    )


def packet(i, channel_id=101):
    return raw.types.Updates(
        updates=[
            raw.types.UpdateNewChannelMessage(
                message=raw.types.Message(
                    id=i,
                    peer_id=raw.types.PeerChannel(channel_id=channel_id),
                    from_id=raw.types.PeerUser(user_id=1000 + i),
                    date=1,
                    message="fixture",
                    restriction_reason=[],
                ),
                pts=100 + i,
                pts_count=1,
            )
        ],
        users=[user(1000 + i, True)],
        chats=[channel(channel_id)],
        date=1,
        seq=i,
    )


@pytest.fixture
async def client():
    app = Client("raw-only-admission", in_memory=True, workers=1, skip_updates=True)
    await app.storage.open()
    app.is_connected = True
    app.dispatcher._accepting_updates = True
    app.case_calls, app.case_raw, app.case_parsed = [], [], []

    async def invoke(query, **kwargs):
        assert isinstance(query, raw.functions.updates.GetChannelDifference)
        mid = query.filter.ranges[0].min_id
        app.case_calls.append(mid)
        return raw.types.updates.ChannelDifference(
            pts=query.pts + 1,
            final=True,
            new_messages=[],
            other_updates=[],
            users=[user(1000 + mid)],
            chats=[channel(query.channel.channel_id)],
        )

    async def raw_handler(client, update, users, chats):
        app.case_raw.append((update.message.id, users[1000 + update.message.id].min))

    async def parsed_handler(client, message):
        app.case_parsed.append((message.id, message.from_user.first_name))

    app.session = SimpleNamespace(invoke=invoke)
    app.add_handler(RawUpdateHandler(raw_handler), group=-1)
    app.add_handler(MessageHandler(parsed_handler, filters.chat(-1000000000202)))
    await app.fetch_peers([channel(101), channel(202)])
    try:
        yield app
    finally:
        await app.storage.close()
        app.executor.shutdown(wait=True)


async def drain(app):
    app.dispatcher.updates_queue.put_nowait(None)
    await app.dispatcher.handler_worker()


async def test_unselected_cold_burst_keeps_raw_order_and_pts_without_hydration(client):
    client.update_parser_policy = lambda update: update.message.peer_id.channel_id == 202
    for i in range(1, 41):
        await client.handle_updates(packet(i))
    await client.handle_updates(packet(41, 202))
    await drain(client)
    assert client.case_calls == [41]
    assert [i for i, _ in client.case_raw] == list(range(1, 42))
    assert client.case_parsed == [(41, "Fixture")]
    assert (await client.storage.get_update_states(-1000000000101))[0].pts == 140
    assert (await client.storage.get_update_states(-1000000000202))[0].pts == 141


async def test_default_retains_hydration_and_parsed_metadata(client):
    await client.handle_updates(packet(1, 202))
    await drain(client)
    assert client.case_calls == [1]
    assert client.case_raw == [(1, False)]
    assert client.case_parsed == [(1, "Fixture")]


@pytest.mark.parametrize("value", [None, 0, "false", [], object()])
async def test_malformed_policy_result_preserves_full_delivery(client, value, caplog):
    client.update_parser_policy = lambda update: value
    await client.handle_updates(packet(1, 202))
    await drain(client)
    assert client.case_calls == [1]
    assert client.case_parsed == [(1, "Fixture")]
    assert "update_parser_policy_invalid" in caplog.text


async def test_policy_failure_fails_open_without_exception_payload(client, caplog):
    def broken(update):
        raise ValueError("synthetic-private-payload")

    client.update_parser_policy = broken
    await client.handle_updates(packet(1, 202))
    await drain(client)
    assert client.case_calls == [1]
    assert client.case_parsed == [(1, "Fixture")]
    assert "update_parser_policy_failed" in caplog.text
    assert "synthetic-private-payload" not in caplog.text


async def test_admission_decision_does_not_change_when_routes_change_before_dispatch(client):
    selected = set()
    client.update_parser_policy = lambda update: update.message.peer_id.channel_id in selected
    await client.handle_updates(packet(1, 202))
    selected.add(202)
    await client.handle_updates(packet(2, 202))
    await drain(client)
    assert client.case_calls == [2]
    assert [i for i, _ in client.case_raw] == [1, 2]
    assert client.case_parsed == [(2, "Fixture")]


async def test_mixed_container_retains_each_update_and_each_cursor(client):
    client.update_parser_policy = lambda update: update.message.peer_id.channel_id == 202
    first, second = packet(1), packet(2, 202)
    first.updates.extend(second.updates)
    first.users.extend(second.users)
    first.chats.extend(second.chats)
    await client.handle_updates(first)
    await drain(client)
    assert client.case_calls == [2]
    assert [i for i, _ in client.case_raw] == [1, 2]
    assert client.case_parsed == [(2, "Fixture")]
    assert (await client.storage.get_update_states(-1000000000101))[0].pts == 101
    assert (await client.storage.get_update_states(-1000000000202))[0].pts == 102


async def test_service_update_never_uses_raw_only_policy(client):
    called = []
    client.update_parser_policy = lambda update: called.append(update) or False
    incoming = packet(1, 202)
    incoming.updates[0].message = raw.types.MessageService(
        id=1,
        peer_id=raw.types.PeerChannel(channel_id=202),
        date=1,
        from_id=raw.types.PeerUser(user_id=1001),
        action=raw.types.MessageActionHistoryClear(),
    )
    await client.handle_updates(incoming)
    assert called == []
    assert client.case_calls == [1]
    update, users, chats = client.dispatcher.updates_queue.get_nowait()
    assert update is incoming.updates[0]
    assert users[1001].min is False


async def test_async_policy_is_rejected_without_unawaited_coroutine(client, caplog):
    async def accidental_async(update):
        return False

    client.update_parser_policy = accidental_async
    await client.handle_updates(packet(1, 202))
    await drain(client)
    assert client.case_calls == [1]
    assert client.case_parsed == [(1, "Fixture")]
    assert "result_type=coroutine action=hydrate_and_parse" in caplog.text

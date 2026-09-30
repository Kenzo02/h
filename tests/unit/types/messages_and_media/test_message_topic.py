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

from unittest.mock import AsyncMock

import pytest

from pyrogram import Client, enums, raw, types, utils
from pyrogram.errors import ChannelForumMissing, ChannelPrivate

CHANNEL_ID = 1_000_000_000
GROUP_ID = 42
USER_ID = 777000
DATE = 1_755_100_000
TOPIC_ID = 7


def chat_fixture(kind: str):
    if kind == "group":
        chat = raw.types.Chat(
            id=GROUP_ID,
            title="Group",
            photo=raw.types.ChatPhotoEmpty(),
            participants_count=2,
            date=DATE,
            version=1,
        )
        return (
            raw.types.PeerChat(chat_id=GROUP_ID),
            chat,
            -GROUP_ID,
            raw.types.InputPeerChat(chat_id=GROUP_ID),
            enums.ChatType.GROUP,
        )

    chat = raw.types.Channel(
        id=CHANNEL_ID,
        title=kind.title(),
        photo=raw.types.ChatPhotoEmpty(),
        date=DATE,
        broadcast=kind == "broadcast",
        megagroup=kind == "supergroup",
        forum=kind == "forum",
        access_hash=0,
        usernames=[],
        restriction_reason=[],
    )
    return (
        raw.types.PeerChannel(channel_id=CHANNEL_ID),
        chat,
        utils.get_channel_id(CHANNEL_ID),
        raw.types.InputPeerChannel(channel_id=CHANNEL_ID, access_hash=0),
        enums.ChatType.FORUM
        if kind == "forum"
        else enums.ChatType.CHANNEL
        if kind == "broadcast"
        else enums.ChatType.SUPERGROUP,
    )


def make_client(*, fetch_topics: bool = True, is_bot: bool | None = False) -> Client:
    client = Client("topic-parser-test", api_id=1, api_hash="0" * 32, in_memory=True)
    client.fetch_topics = fetch_topics
    client.me = types.User(client=client, id=USER_ID, is_bot=is_bot) if is_bot is not None else None
    client.get_forum_topics_by_id = AsyncMock(return_value=types.ForumTopic(id=TOPIC_ID))
    return client


def make_message(kind: str, peer: raw.base.Peer, message_id: int, *, with_topic: bool = False):
    reply_to = (
        raw.types.MessageReplyHeader(
            forum_topic=True,
            reply_to_msg_id=TOPIC_ID + 1,
            reply_to_top_id=TOPIC_ID,
        )
        if with_topic
        else None
    )

    if kind == "service":
        return raw.types.MessageService(
            id=message_id,
            peer_id=peer,
            from_id=raw.types.PeerUser(user_id=USER_ID),
            date=DATE,
            action=raw.types.MessageActionCustomAction(message="service"),
            reply_to=reply_to,
        )

    if kind == "ephemeral":
        return raw.types.EphemeralMessage(
            id=message_id,
            from_id=raw.types.PeerUser(user_id=USER_ID),
            receiver_id=USER_ID,
            peer_id=peer,
            date=DATE,
            message="ephemeral",
            reply_to=reply_to,
        )

    return raw.types.Message(
        id=message_id,
        peer_id=peer,
        from_id=raw.types.PeerUser(user_id=USER_ID),
        date=DATE,
        message="ordinary",
        entities=[],
        restriction_reason=[],
        reply_to=reply_to,
    )


async def parse_message(client: Client, kind: str, peer: raw.base.Peer, chat: raw.base.Chat):
    return await types.Message._parse(
        client,
        make_message(kind, peer, message_id=1, with_topic=True),
        users={},
        chats={chat.id: chat},
        replies=0,
    )


@pytest.mark.parametrize(
    ("chat_kind", "expected_chat_type"),
    [
        pytest.param("group", enums.ChatType.GROUP, id="basic-group"),
        pytest.param("broadcast", enums.ChatType.CHANNEL, id="broadcast-channel"),
        pytest.param("supergroup", enums.ChatType.SUPERGROUP, id="nonforum-supergroup"),
    ],
)
@pytest.mark.asyncio
async def test_get_chat_history_does_not_fetch_topics_for_nonforum_batches(
    chat_kind: str,
    expected_chat_type: enums.ChatType,
) -> None:
    peer, chat, chat_id, input_peer, _ = chat_fixture(chat_kind)
    client = make_client()
    client.resolve_peer = AsyncMock(return_value=input_peer)
    client.invoke = AsyncMock(
        return_value=raw.types.messages.Messages(
            messages=[
                make_message("ordinary", peer, message_id=1),
                make_message("service", peer, message_id=2),
            ],
            topics=[],
            chats=[chat],
            users=[],
        )
    )

    parsed_messages = [message async for message in client.get_chat_history(chat_id, limit=2)]

    assert [message.chat.type for message in parsed_messages] == [expected_chat_type] * 2
    assert parsed_messages[0].text == "ordinary"
    assert parsed_messages[1].service == enums.MessageServiceType.CUSTOM_ACTION
    client.invoke.assert_awaited_once()
    assert isinstance(client.invoke.await_args.args[0], raw.functions.messages.GetHistory)
    client.get_forum_topics_by_id.assert_not_awaited()


@pytest.mark.parametrize(
    "chat_kind",
    [
        pytest.param("group", id="basic-group"),
        pytest.param("broadcast", id="broadcast-channel"),
        pytest.param("supergroup", id="nonforum-supergroup"),
    ],
)
@pytest.mark.asyncio
async def test_ephemeral_nonforum_messages_do_not_fetch_topics(chat_kind: str) -> None:
    peer, chat, _, _, _ = chat_fixture(chat_kind)
    client = make_client()

    parsed_message = await types.Message._parse(
        client,
        make_message("ephemeral", peer, message_id=1),
        users={},
        chats={chat.id: chat},
        replies=0,
    )

    assert not parsed_message.chat.is_forum
    assert parsed_message.topic is None
    client.get_forum_topics_by_id.assert_not_awaited()


@pytest.mark.parametrize("kind", ["ordinary", "service", "ephemeral"])
@pytest.mark.asyncio
async def test_forum_messages_still_fetch_and_cache_topics(kind: str) -> None:
    peer, chat, _, _, _ = chat_fixture("forum")
    client = make_client()
    topic = types.ForumTopic(id=TOPIC_ID, title="Forum topic")
    client.get_forum_topics_by_id.return_value = topic

    parsed_message = await parse_message(client, kind, peer, chat)

    assert parsed_message.chat.is_forum
    assert parsed_message.topic is topic
    assert await client.topic_cache.get((parsed_message.chat.id, TOPIC_ID)) is topic
    client.get_forum_topics_by_id.assert_awaited_once_with(
        chat_id=parsed_message.chat.id,
        topic_ids=TOPIC_ID,
    )


@pytest.mark.parametrize("kind", ["ordinary", "service", "ephemeral"])
@pytest.mark.parametrize(
    ("error_name", "is_suppressed"),
    [
        pytest.param("channel-private", True, id="channel-private-is-suppressed"),
        pytest.param("channel-forum-missing", True, id="forum-missing-is-suppressed"),
        pytest.param("network", False, id="other-errors-propagate"),
    ],
)
@pytest.mark.asyncio
async def test_forum_topic_enrichment_keeps_its_error_policy(
    kind: str,
    error_name: str,
    is_suppressed: bool,
) -> None:
    peer, chat, _, _, _ = chat_fixture("forum")
    client = make_client()
    error = {
        "channel-private": lambda: ChannelPrivate("CHANNEL_PRIVATE"),
        "channel-forum-missing": lambda: ChannelForumMissing("CHANNEL_FORUM_MISSING"),
        "network": lambda: RuntimeError("topic request failed"),
    }[error_name]()
    client.get_forum_topics_by_id.side_effect = error

    if is_suppressed:
        parsed_message = await parse_message(client, kind, peer, chat)
        assert parsed_message.topic is None
    else:
        with pytest.raises(RuntimeError, match="topic request failed"):
            await parse_message(client, kind, peer, chat)

    client.get_forum_topics_by_id.assert_awaited_once()


@pytest.mark.parametrize("kind", ["ordinary", "service", "ephemeral"])
@pytest.mark.parametrize(
    ("fetch_topics", "is_bot"),
    [
        pytest.param(False, False, id="fetch-topics-disabled"),
        pytest.param(True, True, id="bot-client"),
        pytest.param(True, None, id="client-identity-unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_forum_topic_fetch_controls_remain_in_effect(
    kind: str,
    fetch_topics: bool,
    is_bot: bool | None,
) -> None:
    peer, chat, _, _, _ = chat_fixture("forum")
    client = make_client(fetch_topics=fetch_topics, is_bot=is_bot)

    parsed_message = await parse_message(client, kind, peer, chat)

    assert parsed_message.topic is None
    client.get_forum_topics_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_chat_history_propagates_unexpected_forum_topic_errors() -> None:
    peer, chat, chat_id, input_peer, _ = chat_fixture("forum")
    client = make_client()
    client.resolve_peer = AsyncMock(return_value=input_peer)
    client.invoke = AsyncMock(
        return_value=raw.types.messages.Messages(
            messages=[make_message("ordinary", peer, message_id=1, with_topic=True)],
            topics=[],
            chats=[chat],
            users=[],
        )
    )
    client.get_forum_topics_by_id.side_effect = RuntimeError("topic request failed")

    with pytest.raises(RuntimeError, match="topic request failed"):
        [message async for message in client.get_chat_history(chat_id, limit=1)]

    client.get_forum_topics_by_id.assert_awaited_once()

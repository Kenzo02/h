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

import pytest

import pyrogram
from pyrogram import enums, raw, types

CHANNEL_ID = 1000000000
USER_ID = 777000
DATE = 1755100000


def client():
    return pyrogram.Client("test", api_id=1, api_hash="0" * 32, in_memory=True)


def monoforum_chat():
    return raw.types.Channel(
        id=CHANNEL_ID,
        title="Direct messages",
        photo=raw.types.ChatPhotoEmpty(),
        date=DATE,
        broadcast=True,
        monoforum=True,
        access_hash=0,
        usernames=[],
        restriction_reason=[]
    )


def message(*, saved_peer_id=None):
    return raw.types.Message(
        id=1,
        peer_id=raw.types.PeerChannel(channel_id=CHANNEL_ID),
        from_id=raw.types.PeerUser(user_id=USER_ID),
        saved_peer_id=saved_peer_id,
        date=DATE,
        message="hi",
        entities=[],
        restriction_reason=[]
    )


class CapturingClient:
    async def edit_message_text(self, **kwargs):
        self.edit_message_text_kwargs = kwargs
        return "edited"


@pytest.mark.asyncio
async def test_a_direct_message_without_a_topic_parses():
    parsed = await types.Message._parse(
        client(),
        message(),
        users={},
        chats={CHANNEL_ID: monoforum_chat()}
    )

    assert parsed.chat.type == enums.ChatType.DIRECT
    assert parsed.direct_messages_topic_id is None
    assert parsed.topic is None


@pytest.mark.asyncio
async def test_a_direct_message_with_a_topic_keeps_its_id():
    parsed = await types.Message._parse(
        client(),
        message(saved_peer_id=raw.types.PeerUser(user_id=USER_ID)),
        users={},
        chats={CHANNEL_ID: monoforum_chat()}
    )

    assert parsed.direct_messages_topic_id == USER_ID


@pytest.mark.asyncio
async def test_message_edit_text_preserves_legacy_positional_forwarding():
    client = CapturingClient()
    bound_message = types.Message(client=client, id=1, chat=types.Chat(id=CHANNEL_ID))
    reply_markup = object()
    rich_message = object()

    result = await bound_message.edit_text(
        "updated",
        None,
        None,
        None,
        reply_markup,
        True,
        False,
        rich_message=rich_message,
    )

    assert result == "edited"
    assert client.edit_message_text_kwargs == {
        "chat_id": CHANNEL_ID,
        "message_id": 1,
        "text": "updated",
        "parse_mode": None,
        "entities": None,
        "link_preview_options": None,
        "business_connection_id": None,
        "reply_markup": reply_markup,
        "rich_message": rich_message,
        "show_caption_above_media": True,
        "disable_web_page_preview": False,
    }

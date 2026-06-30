import pytest

import pyrogram
from pyrogram import raw, types
from pyrogram.methods.chats.join_chat import JoinChat


class Client(JoinChat):
    INVITE_LINK_RE = pyrogram.Client.INVITE_LINK_RE

    def __init__(self, result):
        self.result = result
        self.query = None
        self.resolved = []

    async def resolve_peer(self, chat_id):
        self.resolved.append(chat_id)
        return raw.types.InputChannel(channel_id=123, access_hash=456)

    async def invoke(self, query):
        self.query = query
        return self.result


def raw_channel():
    return raw.types.Channel(
        id=123,
        title="channel",
        photo=raw.types.ChatPhotoEmpty(),
        date=0,
        megagroup=True,
    )


def raw_chat():
    return raw.types.Chat(
        id=123,
        title="chat",
        photo=raw.types.ChatPhotoEmpty(),
        participants_count=1,
        date=0,
        version=1,
    )


def updates(chat):
    return raw.types.Updates(updates=[], users=[], chats=[chat], date=0, seq=0)


@pytest.mark.asyncio
async def test_join_chat_username_returns_success_result_from_updates():
    client = Client(updates(raw_channel()))

    result = await client.join_chat("channel")

    assert isinstance(client.query, raw.functions.channels.JoinChannel)
    assert client.resolved == ["channel"]
    assert isinstance(result, types.ChatJoinResultSuccess)
    assert result.chat.title == "channel"


@pytest.mark.asyncio
async def test_join_chat_invite_link_returns_success_result_from_join_result_ok():
    client = Client(raw.types.messages.ChatInviteJoinResultOk(updates=updates(raw_chat())))

    result = await client.join_chat("https://t.me/+AbCdEf0123456789")

    assert isinstance(client.query, raw.functions.messages.ImportChatInvite)
    assert client.resolved == []
    assert isinstance(result, types.ChatJoinResultSuccess)
    assert result.chat.title == "chat"

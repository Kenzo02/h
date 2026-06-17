import pytest

from pyrogram import raw, types
from pyrogram.methods.users.set_emoji_status import SetEmojiStatus


class Client(SetEmojiStatus):
    def __init__(self, peer=None):
        self.peer = peer or raw.types.InputPeerSelf()
        self.resolved = []
        self.query = None

    async def resolve_peer(self, chat_id):
        self.resolved.append(chat_id)
        return self.peer

    async def invoke(self, query):
        self.query = query
        return True


@pytest.mark.asyncio
async def test_set_emoji_status_keeps_positional_status_compatibility():
    client = Client()
    status = types.EmojiStatus(custom_emoji_id="123")

    assert await client.set_emoji_status(status) is True

    assert client.resolved == []
    assert isinstance(client.query, raw.functions.account.UpdateEmojiStatus)
    assert isinstance(client.query.emoji_status, raw.types.EmojiStatus)


@pytest.mark.asyncio
async def test_set_emoji_status_supports_channel_status():
    client = Client(raw.types.InputPeerChannel(channel_id=123, access_hash=456))
    status = types.EmojiStatus(custom_emoji_id="123")

    assert await client.set_emoji_status("channel", emoji_status=status) is True

    assert client.resolved == ["channel"]
    assert isinstance(client.query, raw.functions.channels.UpdateEmojiStatus)
    assert client.query.channel.channel_id == 123

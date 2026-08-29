import pytest

from pyrogram import raw
from pyrogram.methods.bots.set_game_score import SetGameScore


class Client(SetGameScore):
    def __init__(self):
        self.resolved = []
        self.query = None

    async def resolve_peer(self, value):
        self.resolved.append(value)
        if value == "chat":
            return raw.types.InputPeerSelf()
        return raw.types.InputUserSelf()

    async def invoke(self, query):
        self.query = query
        return raw.types.Updates(updates=[], users=[], chats=[], date=0, seq=0)


@pytest.mark.asyncio
async def test_set_game_score_preserves_legacy_named_chat_message_arguments():
    client = Client()

    assert await client.set_game_score("user", 100, chat_id="chat", message_id=7) is True

    assert client.resolved == ["chat", "user"]
    assert client.query.id == 7
    assert client.query.score == 100


@pytest.mark.asyncio
async def test_set_game_score_preserves_legacy_all_positional_mapping():
    client = Client()

    await client.set_game_score("user", 900, True, True, "chat", 17)

    assert client.resolved == ["chat", "user"]
    assert client.query.id == 17
    assert client.query.score == 900
    assert client.query.force is True
    assert client.query.edit_message is None

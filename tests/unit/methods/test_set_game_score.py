import pytest

from pyrogram import raw, types, utils
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
    assert client.query.edit_message is False


@pytest.mark.asyncio
async def test_set_game_score_returns_the_edit_not_an_unrelated_new_message(monkeypatch):
    client = Client()
    new_message = raw.types.MessageEmpty(id=8)
    edited_message = raw.types.MessageEmpty(id=7)
    response = raw.types.Updates(
        updates=[
            raw.types.UpdateNewMessage(message=new_message, pts=1, pts_count=1),
            raw.types.UpdateEditMessage(message=edited_message, pts=2, pts_count=1),
        ],
        users=[],
        chats=[],
        date=0,
        seq=0,
    )

    async def invoke(query):
        return response

    async def parse(client, message, users, chats):
        assert message is edited_message
        return "edited"

    monkeypatch.setattr(client, "invoke", invoke)
    monkeypatch.setattr(types.Message, "_parse", parse)
    assert await client.set_game_score("user", 100, chat_id="chat", message_id=7) == "edited"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [True, False])
async def test_set_game_score_inline_uses_its_dc_and_preserves_boolean(monkeypatch, result):
    client = Client()
    inline_id = raw.types.InputBotInlineMessageID(dc_id=4, id=123, access_hash=456)
    calls = []

    async def invoke(query):
        assert isinstance(query, raw.functions.messages.SetInlineGameScore)
        assert query.id is inline_id
        assert query.score == 100
        return result

    async def get_session(dc_id, *, is_media):
        calls.append((dc_id, is_media))
        return type("Session", (), {"invoke": staticmethod(invoke)})()

    monkeypatch.setattr(utils, "unpack_inline_message_id", lambda value: inline_id)
    monkeypatch.setattr(client, "get_session", get_session, raising=False)
    assert await client.set_game_score("user", 100, inline_message_id="inline") is result
    assert calls == [(4, True)]


@pytest.mark.asyncio
async def test_set_game_score_requires_a_complete_normal_target():
    with pytest.raises(ValueError, match="chat_id and message_id"):
        await Client().set_game_score("user", 100, chat_id="chat")

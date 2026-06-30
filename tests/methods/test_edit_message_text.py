import pytest

from pyrogram import raw
from pyrogram.methods.messages.edit_inline_text import EditInlineText
from pyrogram.methods.messages.edit_message_text import EditMessageText


class MessageClient(EditMessageText):
    def __init__(self):
        self.link_preview_options = None
        self.query = None

    async def resolve_peer(self, chat_id):
        return raw.types.InputPeerSelf()

    async def invoke(self, query, **kwargs):
        self.query = query
        return raw.types.Updates(updates=[], users=[], chats=[], date=0, seq=0)


class InlineSession:
    def __init__(self):
        self.query = None

    async def invoke(self, query, **kwargs):
        self.query = query
        return True


class InlineClient(EditInlineText):
    def __init__(self):
        self.link_preview_options = None
        self.sleep_threshold = 0
        self.session = InlineSession()

    async def get_session(self, dc_id, is_media=False):
        return self.session


async def parse_text_entities(client, text, parse_mode, entities):
    return {"message": text, "entities": entities}


@pytest.mark.asyncio
async def test_edit_message_text_preserves_custom_entities(monkeypatch):
    custom_entities = [raw.types.MessageEntityBold(offset=0, length=4)]
    client = MessageClient()

    monkeypatch.setattr(
        "pyrogram.methods.messages.edit_message_text.utils.parse_text_entities",
        parse_text_entities,
    )

    await client.edit_message_text("me", 1, "bold", entities=custom_entities)

    assert client.query.entities is custom_entities


@pytest.mark.asyncio
async def test_edit_inline_text_preserves_custom_entities(monkeypatch):
    custom_entities = [raw.types.MessageEntityBold(offset=0, length=4)]
    client = InlineClient()

    monkeypatch.setattr(
        "pyrogram.methods.messages.edit_inline_text.utils.parse_text_entities",
        parse_text_entities,
    )
    monkeypatch.setattr(
        "pyrogram.methods.messages.edit_inline_text.utils.unpack_inline_message_id",
        lambda _: raw.types.InputBotInlineMessageID(dc_id=1, id=2, access_hash=3),
    )

    await client.edit_inline_text("inline-id", "bold", entities=custom_entities)

    assert client.session.query.entities is custom_entities

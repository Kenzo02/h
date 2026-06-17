from types import SimpleNamespace

import pytest

from pyrogram import raw
from pyrogram.methods.messages.send_voice import SendVoice


class Client(SendVoice):
    def __init__(self):
        self.query = None

    def rnd_id(self):
        return 1

    async def resolve_peer(self, chat_id):
        return raw.types.InputPeerSelf()

    async def invoke(self, query, **kwargs):
        self.query = query
        return raw.types.Updates(updates=[], users=[], chats=[], date=0, seq=0)

    def guess_mime_type(self, path):
        return "audio/ogg"

    async def save_file(self, *args, **kwargs):
        return SimpleNamespace(id=1)


@pytest.mark.asyncio
async def test_send_voice_keeps_positional_disable_notification_compatibility(monkeypatch):
    async def parse_messages(**kwargs):
        return []

    async def parse_text_entities(*args, **kwargs):
        return {"message": "", "entities": None}

    monkeypatch.setattr("pyrogram.methods.messages.send_voice.utils.parse_messages", parse_messages)
    monkeypatch.setattr("pyrogram.methods.messages.send_voice.utils.parse_text_entities", parse_text_entities)

    client = Client()

    await client.send_voice("me", "https://example.com/voice.ogg", "", None, None, 0, True)

    assert client.query.silent is True


@pytest.mark.asyncio
async def test_send_voice_preserves_waveform_keyword_for_upload(monkeypatch, tmp_path):
    async def parse_messages(**kwargs):
        return []

    async def parse_text_entities(*args, **kwargs):
        return {"message": "", "entities": None}

    monkeypatch.setattr("pyrogram.methods.messages.send_voice.utils.parse_messages", parse_messages)
    monkeypatch.setattr("pyrogram.methods.messages.send_voice.utils.parse_text_entities", parse_text_entities)

    path = tmp_path / "voice.ogg"
    path.write_bytes(b"voice")

    client = Client()
    waveform = b"\x01\x02\x03"

    await client.send_voice("me", str(path), waveform=waveform)

    [attribute] = client.query.media.attributes

    assert attribute.waveform == waveform

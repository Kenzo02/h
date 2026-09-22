from types import SimpleNamespace

import pytest

from pyrogram import raw, types
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
    monkeypatch.setattr(
        "pyrogram.methods.messages.send_voice.utils.parse_text_entities", parse_text_entities
    )

    client = Client()

    await client.send_voice("me", "https://example.com/voice.ogg", "", None, None, 0, True)

    assert client.query.silent is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "waveform", [b"\x01\x02\x03", bytearray(b"\x01\x02\x03"), memoryview(b"\x01\x02\x03")]
)
async def test_send_voice_preserves_waveform_keyword_for_upload(monkeypatch, tmp_path, waveform):
    async def parse_messages(**kwargs):
        return []

    async def parse_text_entities(*args, **kwargs):
        return {"message": "", "entities": None}

    monkeypatch.setattr("pyrogram.methods.messages.send_voice.utils.parse_messages", parse_messages)
    monkeypatch.setattr(
        "pyrogram.methods.messages.send_voice.utils.parse_text_entities", parse_text_entities
    )

    path = tmp_path / "voice.ogg"
    path.write_bytes(b"voice")

    client = Client()
    await client.send_voice("me", str(path), waveform=waveform)

    [attribute] = client.query.media.attributes

    assert attribute.waveform == bytes(waveform)


@pytest.mark.asyncio
async def test_send_voice_adapts_legacy_ephemeral_parameters(monkeypatch):
    async def parse_messages(**kwargs):
        return []

    async def parse_text_entities(*args, **kwargs):
        return {"message": "", "entities": None}

    monkeypatch.setattr("pyrogram.methods.messages.send_voice.utils.parse_messages", parse_messages)
    monkeypatch.setattr(
        "pyrogram.methods.messages.send_voice.utils.parse_text_entities", parse_text_entities
    )

    client = Client()

    await client.send_voice(
        "me",
        "https://example.com/voice.ogg",
        receiver_user_id="receiver",
        callback_query_id="42",
    )

    assert isinstance(client.query, raw.functions.ephemeral.SendMessage)
    assert client.query.query_id == 42


@pytest.mark.asyncio
async def test_send_voice_rejects_legacy_and_new_ephemeral_parameters_together():
    client = Client()
    parameters = types.EphemeralMessageParameters(receiver_user_id="new-receiver")

    with pytest.raises(ValueError, match="cannot be combined"):
        await client.send_voice(
            "me",
            "https://example.com/voice.ogg",
            receiver_user_id="legacy-receiver",
            ephemeral_message_parameters=parameters,
        )

    assert client.query is None

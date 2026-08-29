import pytest

from pyrogram import raw, types
from pyrogram.methods.messages.edit_message_media import EditMessageMedia


class Client(EditMessageMedia):
    async def resolve_peer(self, chat_id):
        return raw.types.InputPeerSelf()

    async def invoke(self, *args, **kwargs):
        return raw.types.Updates(
            updates=[],
            users=[],
            chats=[],
            date=0,
            seq=0,
        )


@pytest.mark.asyncio
async def test_edit_message_media_keeps_file_name_compatibility():
    media = types.InputMediaDocument("document-file-id")
    media.caption = None
    seen = {}

    async def write(**kwargs):
        seen["file_name"] = media.file_name
        return raw.types.InputMediaEmpty()

    media.write = write

    await Client().edit_message_media(
        "me",
        1,
        media,
        file_name="custom.bin",
    )

    assert seen["file_name"] == "custom.bin"

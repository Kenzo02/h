from types import SimpleNamespace

import pytest

from pyrogram import enums, raw, types
from pyrogram.methods.chats.get_top_chats import GetTopChats


class Client(GetTopChats):
    async def invoke(self, *args, **kwargs):
        return raw.types.contacts.TopPeers(
            categories=[
                raw.types.TopPeerCategoryPeers(
                    category=raw.types.TopPeerCategoryCorrespondents(),
                    count=1,
                    peers=[
                        raw.types.TopPeer(
                            peer=raw.types.PeerChat(chat_id=123),
                            rating=1.0,
                        )
                    ],
                )
            ],
            chats=[SimpleNamespace(id=123, title="Group")],
            users=[],
        )


@pytest.mark.asyncio
async def test_get_top_chats_uses_chat_lookup(monkeypatch):
    parsed = []

    def parse_chat(client, chat):
        parsed.append(chat)
        return chat

    monkeypatch.setattr(types.Chat, "_parse_chat", staticmethod(parse_chat))

    result = [
        chat async for chat in Client().get_top_chats(enums.TopChatCategory.GROUPS, limit=1)
    ]

    assert len(result) == 1
    assert result[0].id == 123
    assert parsed[0].title == "Group"

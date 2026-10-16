from __future__ import annotations as _annotations

from unittest.mock import AsyncMock

import pytest

from pyrogram import Client, enums, raw, types


@pytest.mark.asyncio
async def test_embedded_business_reply_is_preserved_without_reply_fetching():
    client = Client("business-reply", in_memory=True, fetch_replies=False, fetch_topics=False)
    client.get_messages = AsyncMock(side_effect=AssertionError("must not fetch replies"))
    peer = raw.types.PeerUser(user_id=7)
    reply = raw.types.Message(id=1, peer_id=peer, date=1, message="original", restriction_reason=[])
    message = raw.types.Message(
        id=2,
        peer_id=peer,
        date=2,
        message="answer",
        restriction_reason=[],
        reply_to=raw.types.MessageReplyHeader(reply_to_msg_id=1),
    )
    parsed = await types.Message._parse(
        client,
        message,
        users={7: raw.types.User(id=7, first_name="Example")},
        chats={},
        replies=0,
        raw_reply_to_message=reply,
        business_connection_id="connection",
    )
    assert parsed.reply_to_message.id == 1
    assert parsed.reply_to_message.text == "original"
    assert parsed.reply_to_message.business_connection_id == "connection"
    client.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_verification_description_preserves_text_and_entities():
    client = Client("verification", in_memory=True)
    verification = raw.types.BotVerification(
        bot_id=7,
        icon=123,
        description=raw.types.TextWithEntities(
            text="verified",
            entities=[raw.types.MessageEntityBold(offset=0, length=8)],
        ),
    )
    parsed = await types.BotVerification._parse(client, verification, users={})
    assert str(parsed.description) == "verified"
    assert parsed.description.text == "verified"
    assert parsed.description.entities is not None
    assert parsed.description.entities[0].type == enums.MessageEntityType.BOLD


def test_ton_exports_are_additive_and_do_not_change_client_defaults():
    client = Client("wallet-exports", in_memory=True)
    for method in (
        "create_user_wallet",
        "delete_ton_wallet",
        "get_address_ton_wallet",
        "get_ton_wallet_ownership_proof_challenge",
        "get_user_ton_wallet_addresses",
        "replace_ton_wallet",
    ):
        assert callable(getattr(client, method))
    assert types.RichTextTonAddress
    assert types.TonWalletTransfer
    assert types.TonConnectRequest
    assert raw.all.layer == 230
    assert raw.types.MessageEntityTonAddress(offset=0, length=1).write()

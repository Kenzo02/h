from __future__ import annotations as _annotations

import inspect
import pickle
from unittest.mock import AsyncMock

import pytest

import pyrogram
from pyrogram import raw, types, utils
from pyrogram.client import Cache

from pyrogram.storage import SQLiteStorage

pytestmark = pytest.mark.unit

# The released fork's positional constructor ABI, independently fixed as a contract.
LEGACY_CLIENT_PARAMETERS = (
    "name api_id api_hash app_version device_model system_version lang_pack lang_code "
    "system_lang_code ipv6 proxy test_mode bot_token session_string in_memory phone_number "
    "phone_code password workers workdir plugins parse_mode no_updates skip_updates takeout "
    "sleep_threshold hide_password max_concurrent_transmissions max_message_cache_size "
    "max_topic_cache_size storage_engine client_platform link_preview_options fetch_replies "
    "fetch_topics fetch_stories fetch_stickers init_connection_params connection_factory protocol_factory"
).split()


def test_client_constructor_keeps_every_existing_positional_slot():
    parameters = list(inspect.signature(pyrogram.Client).parameters.values())
    assert [
        parameter.name for parameter in parameters[: len(LEGACY_CLIENT_PARAMETERS)]
    ] == LEGACY_CLIENT_PARAMETERS
    args = [parameter.default for parameter in parameters[: len(LEGACY_CLIENT_PARAMETERS)]]
    storage = SQLiteStorage("legacy-positional", workdir=pyrogram.Client.WORKDIR, in_memory=True)
    args[0] = "legacy-positional"
    args[LEGACY_CLIENT_PARAMETERS.index("storage_engine")] = storage
    args[LEGACY_CLIENT_PARAMETERS.index("fetch_topics")] = False
    client = pyrogram.Client(*args)
    assert client.storage is storage
    assert client.fetch_topics is False
    assert (
        client.protocol_factory
        is parameters[LEGACY_CLIENT_PARAMETERS.index("protocol_factory")].default
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["edit_message_text", "edit_message_media", "edit_message_reply_markup"]
)
@pytest.mark.parametrize(
    "edit_type", [raw.types.UpdateEditMessage, raw.types.UpdateEditChannelMessage]
)
async def test_edits_return_requested_message_not_unrelated_updates(method, edit_type):
    client = pyrogram.Client("mixed-edit", in_memory=True)
    client.resolve_peer = AsyncMock(return_value=raw.types.InputPeerSelf())
    client.invoke = AsyncMock(
        return_value=raw.types.Updates(
            updates=[
                raw.types.UpdateNewMessage(
                    message=raw.types.MessageEmpty(id=99), pts=1, pts_count=1
                ),
                edit_type(message=raw.types.MessageEmpty(id=8), pts=2, pts_count=1),
                edit_type(message=raw.types.MessageEmpty(id=7), pts=3, pts_count=1),
            ],
            users=[],
            chats=[],
            date=0,
            seq=0,
        )
    )
    kwargs = {"text": "changed"} if method == "edit_message_text" else {}
    if method == "edit_message_media":
        kwargs["media"] = types.InputMediaPhoto("https://example.invalid/photo.jpg")
    result = await getattr(client, method)("me", 7, **kwargs)
    assert result.id == 7


@pytest.mark.asyncio
async def test_edit_without_target_raises_instead_of_returning_another_message():
    client = pyrogram.Client("missing-edit", in_memory=True)
    response = raw.types.Updates(
        updates=[
            raw.types.UpdateNewMessage(message=raw.types.MessageEmpty(id=7), pts=1, pts_count=1)
        ],
        users=[],
        chats=[],
        date=0,
        seq=0,
    )
    with pytest.raises(ValueError, match="no edited message"):
        await utils.parse_edited_message(client, response, 7)


def test_send_rich_message_keeps_positional_slots_before_new_business_parameter():
    signature = inspect.signature(pyrogram.Client.send_rich_message)
    old_names = (
        "self chat_id rich_message disable_notification message_thread_id "
        "direct_messages_topic_id ephemeral_message_parameters effect_id reply_parameters "
        "protect_content allow_paid_broadcast suggested_post_parameters reply_markup"
    ).split()
    assert list(signature.parameters)[: len(old_names)] == old_names
    bound = signature.bind(
        None,
        "me",
        types.InputRichMessage(html="<p>text</p>"),
        None,
        None,
        None,
        None,
        None,
        None,
        True,
        False,
        None,
        None,
        business_connection_id="connection",
    )
    assert bound.arguments["protect_content"] is True
    assert bound.arguments["allow_paid_broadcast"] is False
    assert bound.arguments["business_connection_id"] == "connection"


def test_old_cache_import_uses_the_single_new_owner():
    assert Cache is utils.Cache
    client = pyrogram.Client(
        "disabled-cache", in_memory=True, max_message_cache_size=0, max_topic_cache_size=0
    )
    assert isinstance(client.message_cache, Cache)
    assert client.message_cache.capacity == 0
    assert client.topic_cache.capacity == 0


@pytest.mark.asyncio
async def test_zero_capacity_sticker_cache_can_be_disabled():
    client = pyrogram.Client(
        "disabled-sticker-cache", in_memory=True, max_sticker_set_name_cache_size=0
    )
    await client.sticker_set_name_cache.set((1, 2), "name")
    assert await client.sticker_set_name_cache.get((1, 2)) is None


def test_forum_topic_old_and_new_names_share_the_same_values():
    old = types.ForumTopic(id=7, title="Topic", icon_emoji_id=123)
    new = types.ForumTopic(id=7, name="Topic", icon_custom_emoji_id="123")
    assert old == new
    assert old.title == old.name == "Topic"
    assert old.icon_emoji_id == 123
    old.title = "Renamed"
    assert old.name == "Renamed"
    old.icon_emoji_id = None
    assert old.icon_custom_emoji_id is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "old", "name": "different"},
        {"icon_emoji_id": 1, "icon_custom_emoji_id": "2"},
    ],
)
def test_conflicting_topic_aliases_are_not_silently_selected(kwargs):
    with pytest.raises(ValueError):
        types.ForumTopic(id=7, **kwargs)


def test_old_cached_topic_restores_both_contracts():
    cached = types.ForumTopic.__new__(types.ForumTopic)
    cached.__setstate__({"id": 7, "title": "Cached", "icon_emoji_id": 123, "_client": None})
    # The pickle payload is generated from this isolated test fixture, never external data.
    restored = pickle.loads(pickle.dumps(cached))
    assert restored.title == restored.name == "Cached"
    assert restored.icon_emoji_id == 123
    assert restored.icon_custom_emoji_id == "123"
    assert "title" not in restored.__dict__


@pytest.mark.asyncio
async def test_topic_parse_keeps_missing_icon_optional():
    topic = raw.types.ForumTopic(
        id=7,
        date=0,
        peer=raw.types.PeerChannel(channel_id=1),
        title="Topic",
        icon_color=0,
        top_message=7,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=raw.types.PeerUser(user_id=2),
        notify_settings=raw.types.PeerNotifySettings(),
    )
    parsed = await types.ForumTopic._parse(None, topic)
    assert parsed.name == parsed.title == "Topic"
    assert parsed.icon_custom_emoji_id is None
    assert parsed.icon_emoji_id is None
    assert parsed.creator is None


@pytest.mark.asyncio
async def test_topic_edit_without_icon_does_not_create_a_none_string():
    message = raw.types.MessageService(
        id=8,
        peer_id=raw.types.PeerChannel(channel_id=1),
        date=0,
        action=raw.types.MessageActionTopicEdit(title="Renamed"),
        reply_to=raw.types.MessageReplyHeader(reply_to_msg_id=7, reply_to_top_id=7),
    )
    topic = await types.ForumTopic._parse_message(None, message)
    assert topic.id == 7
    assert topic.name == "Renamed"
    assert topic.icon_custom_emoji_id is None


@pytest.mark.asyncio
async def test_get_stickers_preserves_old_short_name_and_list_return(monkeypatch):
    client = pyrogram.Client("legacy-stickers", in_memory=True)
    client.invoke = AsyncMock(
        return_value=raw.types.messages.StickerSet(
            set=raw.types.StickerSet(
                id=1, access_hash=2, title="Set", short_name="set", count=0, hash=0
            ),
            packs=[],
            keywords=[],
            documents=[],
        )
    )
    result = await client.get_stickers(short_name="set")
    assert isinstance(result, types.List)
    assert result == []
    assert client.invoke.call_args.args[0].stickerset.short_name == "set"


def test_new_methods_are_connected_to_client():
    for name in (
        "get_app_config",
        "edit_folder_invite_link",
        "set_bot_profile_photo",
        "create_new_sticker_set",
        "add_sticker_to_set",
        "get_sticker_set",
    ):
        assert callable(getattr(pyrogram.Client, name))

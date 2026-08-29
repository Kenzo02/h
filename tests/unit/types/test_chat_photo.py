#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

from types import SimpleNamespace

import pytest

from pyrogram import enums, raw, types

PHOTO_ID = 123
ACCESS_HASH = 456
DATE = 1755100000
DC_ID = 4
PEER_ID = 789
PEER_ACCESS_HASH = 987


class StickerSetClient:
    async def invoke(self, query):
        return SimpleNamespace(set=SimpleNamespace(short_name="photo-set"))


def photo(video_sizes):
    return raw.types.Photo(
        id=PHOTO_ID,
        access_hash=ACCESS_HASH,
        file_reference=b"reference",
        date=DATE,
        sizes=[raw.types.PhotoSize(type="m", w=100, h=100, size=10)],
        dc_id=DC_ID,
        video_sizes=video_sizes,
    )


@pytest.mark.asyncio
async def test_emoji_markup_only_photo_keeps_sticker_without_animation():
    parsed = await types.ChatPhoto._parse(
        client=None,
        chat_photo=photo(
            [raw.types.VideoSizeEmojiMarkup(emoji_id=112233, background_colors=[])]
        ),
        peer_id=PEER_ID,
        peer_access_hash=PEER_ACCESS_HASH,
    )

    assert parsed.animation is None
    assert parsed.sticker.type == enums.ChatPhotoStickerType.CUSTOM_EMOJI
    assert parsed.sticker.custom_emoji_id == "112233"


@pytest.mark.asyncio
async def test_sticker_markup_only_photo_keeps_sticker_without_animation():
    parsed = await types.ChatPhoto._parse(
        client=StickerSetClient(),
        chat_photo=photo(
            [
                raw.types.VideoSizeStickerMarkup(
                    stickerset=raw.types.InputStickerSetID(id=1122, access_hash=3344),
                    sticker_id=5566,
                    background_colors=[],
                )
            ]
        ),
        peer_id=PEER_ID,
        peer_access_hash=PEER_ACCESS_HASH,
    )

    assert parsed.animation is None
    assert parsed.sticker.type == enums.ChatPhotoStickerType.REGULAR_OR_MASK
    assert parsed.sticker.set_name == "photo-set"
    assert parsed.sticker.sticker_id == 5566


@pytest.mark.asyncio
async def test_photo_with_video_size_keeps_animation_and_sticker_semantics():
    parsed = await types.ChatPhoto._parse(
        client=None,
        chat_photo=photo(
            [
                raw.types.VideoSizeEmojiMarkup(emoji_id=112233, background_colors=[]),
                raw.types.VideoSize(
                    type="v",
                    w=512,
                    h=512,
                    size=42,
                    video_start_ts=1.25,
                ),
            ]
        ),
        peer_id=PEER_ID,
        peer_access_hash=PEER_ACCESS_HASH,
    )

    assert parsed.animation.length == 512
    assert parsed.animation.animation.width == 512
    assert parsed.animation.animation.height == 512
    assert parsed.animation.animation.file_size == 42
    assert parsed.animation.main_frame_timestamp == 1.25
    assert parsed.sticker.type == enums.ChatPhotoStickerType.CUSTOM_EMOJI
    assert parsed.sticker.custom_emoji_id == "112233"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_photo", "has_animation", "is_personal"),
    [
        (
            raw.types.UserProfilePhoto(
                photo_id=PHOTO_ID,
                dc_id=DC_ID,
                has_video=True,
                personal=True,
            ),
            True,
            True,
        ),
        (
            raw.types.UserProfilePhoto(
                photo_id=PHOTO_ID,
                dc_id=DC_ID,
                has_video=False,
                personal=False,
            ),
            False,
            False,
        ),
        (
            raw.types.ChatPhoto(photo_id=PHOTO_ID, dc_id=DC_ID, has_video=True),
            True,
            None,
        ),
        (
            raw.types.ChatPhoto(photo_id=PHOTO_ID, dc_id=DC_ID, has_video=False),
            False,
            None,
        ),
        (photo([]), None, None),
    ],
)
async def test_chat_photo_preserves_legacy_flags(
    chat_photo, has_animation, is_personal
):
    parsed = await types.ChatPhoto._parse(
        client=None,
        chat_photo=chat_photo,
        peer_id=PEER_ID,
        peer_access_hash=PEER_ACCESS_HASH,
    )

    assert parsed.has_animation is has_animation
    assert parsed.is_personal is is_personal


def test_chat_photo_constructor_keeps_legacy_fields():
    parsed = types.ChatPhoto(
        small_file_id="small",
        small_photo_unique_id="small-unique",
        big_file_id="big",
        big_photo_unique_id="big-unique",
        has_animation=True,
        is_personal=False,
    )

    assert parsed.has_animation is True
    assert parsed.is_personal is False
    assert parsed.animation is None
    assert parsed.sticker is None

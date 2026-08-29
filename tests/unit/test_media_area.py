import pytest

from pyrogram import enums
from pyrogram.types import MediaArea, Reaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("custom_emoji_id", "expected_document_id"),
    [
        ("5319161050128459957", 5319161050128459957),
        (str((1 << 64) - 1), -1),
    ],
)
async def test_media_area_reaction_normalizes_custom_emoji_id(
    custom_emoji_id,
    expected_document_id,
):
    area = MediaArea(
        x=50,
        y=50,
        width=10,
        height=10,
        rotation=0,
        type=enums.MediaAreaType.REACTION,
        reaction=Reaction(custom_emoji_id=custom_emoji_id),
    )

    result = await area.write(None)

    assert result.reaction.document_id == expected_document_id

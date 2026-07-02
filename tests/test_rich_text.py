import pytest

from pyrogram import raw
from pyrogram.types.messages_and_media.rich_text import (
    RichText,
    RichTextBold,
    RichTextBotCommand,
    RichTextCashtag,
    RichTextHashtag,
    RichTextMention,
)


def _bold_text(value):
    return raw.types.TextBold(text=raw.types.TextPlain(text=value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_factory", "expected_type", "attribute", "expected_value"),
    [
        (
            lambda: raw.types.TextMention(text=_bold_text("@name")),
            RichTextMention,
            "username",
            "name",
        ),
        (
            lambda: raw.types.TextHashtag(text=_bold_text("#topic")),
            RichTextHashtag,
            "hashtag",
            "topic",
        ),
        (
            lambda: raw.types.TextCashtag(text=_bold_text("$CASH")),
            RichTextCashtag,
            "cashtag",
            "CASH",
        ),
        (
            lambda: raw.types.TextBotCommand(text=_bold_text("/start")),
            RichTextBotCommand,
            "bot_command",
            "start",
        ),
    ],
)
async def test_rich_text_lstrip_fields_accept_nested_formatting(
    raw_factory,
    expected_type,
    attribute,
    expected_value,
):
    result = await RichText._parse(None, raw_factory())

    assert isinstance(result, expected_type)
    assert getattr(result, attribute) == expected_value
    assert isinstance(result.text, RichTextBold)


@pytest.mark.asyncio
async def test_rich_text_lstrip_fields_accept_concat_with_nested_formatting():
    result = await RichText._parse(
        None,
        raw.types.TextMention(
            text=raw.types.TextConcat(
                texts=[
                    raw.types.TextPlain(text="@na"),
                    _bold_text("me"),
                ]
            )
        ),
    )

    assert isinstance(result, RichTextMention)
    assert result.username == "name"

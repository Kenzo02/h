from io import BytesIO

import pytest

from pyrogram import enums, raw, types
from pyrogram.types.bots_and_keyboards.inline_keyboard_button import InlineKeyboardButton


class ButtonClient:
    def __init__(self):
        self.resolved = []

    async def resolve_peer(self, value):
        self.resolved.append(value)
        return raw.types.InputUserSelf()


def test_inline_keyboard_button_preserves_legacy_positional_order():
    web_app = types.WebAppInfo(url="https://example.test/app")
    login_url = types.LoginUrl(url="https://example.test/login")
    callback_game = types.CallbackGame()

    button = InlineKeyboardButton(
        "Button",
        "callback",
        "https://example.test",
        web_app,
        login_url,
        123,
        "query",
        "current",
        callback_game,
        True,
        True,
        "copy",
        "456",
        enums.ButtonStyle.PRIMARY,
    )

    assert button.callback_data == "callback"
    assert button.url == "https://example.test"
    assert button.web_app is web_app
    assert button.login_url is login_url
    assert button.user_id == 123
    assert button.switch_inline_query == "query"
    assert button.switch_inline_query_current_chat == "current"
    assert button.callback_game is callback_game
    assert button.requires_password is True
    assert button.pay is True
    assert button.copy_text == "copy"
    assert button.icon_custom_emoji_id == "456"
    assert button.style is enums.ButtonStyle.PRIMARY


@pytest.mark.asyncio
async def test_inline_keyboard_button_preserves_legacy_callback_position():
    button = InlineKeyboardButton("Back", "back")

    assert button.callback_data == "back"

    written = await button.write(ButtonClient())

    assert isinstance(written.type, raw.types.InlineButtonTypeCallback)
    assert written.type.data == b"back"


@pytest.mark.asyncio
async def test_inline_keyboard_button_serializes_callback_password_flag():
    written = await InlineKeyboardButton(
        "Confirm",
        callback_data="payload",
        requires_password=True,
    ).write(ButtonClient())

    encoded = written.type.write()
    parsed = raw.types.InlineButtonTypeCallback.read(BytesIO(encoded[4:]))

    assert parsed.data == b"payload"
    assert parsed.requires_password is True


@pytest.mark.asyncio
async def test_inline_keyboard_button_uses_chosen_chat_query():
    chosen_chat = types.SwitchInlineQueryChosenChat(
        query="nested",
        allow_user_chats=True,
    )

    written = await InlineKeyboardButton(
        "Choose",
        switch_inline_query_chosen_chat=chosen_chat,
    ).write(ButtonClient())

    assert isinstance(written.type, raw.types.InlineButtonTypeSwitchInline)
    assert written.type.query == "nested"
    assert isinstance(written.type.peer_types[0], raw.types.InlineQueryPeerTypePM)


@pytest.mark.asyncio
async def test_inline_keyboard_button_writes_nested_login_url_fields():
    client = ButtonClient()
    login_url = types.LoginUrl(
        url="https://example.test/login",
        request_write_access=True,
        forward_text="Continue",
        bot_username="login_bot",
    )

    written = await InlineKeyboardButton("Log in", login_url=login_url).write(client)

    assert isinstance(written.type, raw.types.InputInlineButtonTypeUrlAuth)
    assert written.type.url == login_url.url
    assert written.type.request_write_access is True
    assert written.type.fwd_text == login_url.forward_text
    assert client.resolved == ["login_bot"]
    written.type.write()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "copy_text",
    ["copy me", types.CopyTextButton(text="copy me")],
)
async def test_inline_keyboard_button_accepts_string_and_object_copy_text(copy_text):
    written = await InlineKeyboardButton("Copy", copy_text=copy_text).write(ButtonClient())

    assert isinstance(written.type, raw.types.InlineButtonTypeCopy)
    assert written.type.copy_text == "copy me"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_type"),
    [
        ({"callback_data": "callback"}, raw.types.InlineButtonTypeCallback),
        ({"url": "https://example.test"}, raw.types.InlineButtonTypeUrl),
        ({"login_url": types.LoginUrl(url="https://example.test")}, raw.types.InputInlineButtonTypeUrlAuth),
        ({"user_id": 123}, raw.types.InputInlineButtonTypeUserProfile),
        ({"switch_inline_query": "query"}, raw.types.InlineButtonTypeSwitchInline),
        ({"switch_inline_query_current_chat": "current"}, raw.types.InlineButtonTypeSwitchInline),
        (
            {
                "switch_inline_query_chosen_chat": types.SwitchInlineQueryChosenChat(
                    query="chosen",
                    allow_group_chats=True,
                )
            },
            raw.types.InlineButtonTypeSwitchInline,
        ),
        ({"callback_game": types.CallbackGame()}, raw.types.InlineButtonTypeGame),
        ({"pay": True}, raw.types.InlineButtonTypeBuy),
        ({"copy_text": "copy"}, raw.types.InlineButtonTypeCopy),
        ({"disabled": True}, raw.types.InlineButtonTypeDisabled),
        ({"web_app": types.WebAppInfo(url="https://example.test")}, raw.types.InlineButtonTypeWebView),
    ],
)
async def test_inline_keyboard_button_preserves_exclusive_mode_raw_types(kwargs, expected_type):
    written = await InlineKeyboardButton("Button", **kwargs).write(ButtonClient())

    assert isinstance(written.type, expected_type)

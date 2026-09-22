from __future__ import annotations as _annotations

from io import BytesIO

from pyrogram.raw.core.primitives import Bytes, String


def test_string_replaces_an_unpaired_surrogate_when_encoding() -> None:
    assert String("broken\ud800text") == Bytes(b"broken?text")


def test_string_replaces_invalid_utf8_when_decoding() -> None:
    assert String.read(BytesIO(Bytes(b"\xff"))) == "\ufffd"

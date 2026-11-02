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

from __future__ import annotations as _annotations

import importlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

import pyrogram
from pyrogram import types
from pyrogram.file_id import FileType
from pyrogram.methods.messages.download_media import DownloadMedia

if TYPE_CHECKING:
    from pathlib import Path

download_media_module = importlib.import_module("pyrogram.methods.messages.download_media")


class FakeClient(DownloadMedia):
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.packet: tuple[object, ...] | None = None

    async def handle_download(self, packet: tuple[object, ...]) -> None:
        self.packet = packet

    def guess_extension(self, mime_type: str) -> str:
        return ""

    def rnd_id(self) -> str:
        return "test"

    def captured_packet(self) -> tuple[object, ...]:
        assert self.packet is not None
        return self.packet


@pytest.mark.parametrize(
    ("media_file_name", "expected_file_name"),
    [
        ("../outside.txt", "outside.txt"),
        (r"..\outside.txt", "outside.txt"),
        ("D:outside.txt", "outside.txt"),
        ("/absolute/outside.txt", "outside.txt"),
        (r"\\server\share\outside.txt", "outside.txt"),
        ("safe.txt", "safe.txt"),
    ],
)
@pytest.mark.asyncio
async def test_download_media_sanitizes_telegram_file_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_file_name: str,
    expected_file_name: str,
) -> None:
    monkeypatch.setattr(
        download_media_module.FileId,
        "decode",
        lambda _: SimpleNamespace(file_type=FileType.DOCUMENT),
    )
    client = FakeClient(tmp_path)
    media = types.Document(file_id="id", file_unique_id="unique", file_name=media_file_name)

    await cast("pyrogram.Client", client).download_media(media)
    packet = client.captured_packet()

    assert packet[1] == tmp_path / "downloads"
    assert packet[2] == expected_file_name


@pytest.mark.parametrize("media_file_name", [None, "", ".", ".."])
@pytest.mark.parametrize("destination", ["", "chosen/result.bin"])
async def test_missing_media_name_keeps_explicit_destination_or_generates_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_file_name, destination
):
    monkeypatch.setattr(
        download_media_module.FileId,
        "decode",
        lambda _: SimpleNamespace(file_type=FileType.DOCUMENT),
    )
    client = FakeClient(tmp_path)
    media = types.Document(file_id="id", file_unique_id="unique", file_name=media_file_name)
    await cast("pyrogram.Client", client).download_media(media, file_name=destination)
    packet = client.captured_packet()
    if destination:
        assert packet[1] == tmp_path / "chosen"
        assert packet[2] == "result.bin"
    else:
        assert packet[1] == tmp_path / "downloads"
        assert isinstance(packet[2], str)
        assert packet[2].startswith("document_")
        assert packet[2].endswith("_test.zip")

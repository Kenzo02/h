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

from types import SimpleNamespace

import pytest

import pyrogram
from pyrogram import raw, types, utils

pytestmark = pytest.mark.unit

MEDIA_FACTORIES = [
    types.InputMediaPhoto,
    types.InputMediaVideo,
    types.InputMediaAudio,
    types.InputMediaDocument,
    types.InputMediaAnimation,
]


def uploaded_file(big=False, name="fixture.mp4"):
    if big:
        return raw.types.InputFileBig(id=7, parts=1, name=name)
    return raw.types.InputFile(id=7, parts=1, name=name, md5_checksum="")


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", MEDIA_FACTORIES)
@pytest.mark.parametrize("big", [False, True])
async def test_preuploaded_album_file_uses_real_save_file_without_upload_parts(factory, big):
    client = pyrogram.Client("preuploaded-album", in_memory=True)
    client.is_connected = False
    file = uploaded_file(big)
    thumb = uploaded_file(name="thumb.jpg")
    kwargs = {} if factory is types.InputMediaPhoto else {"thumb": thumb}
    if factory in (
        types.InputMediaVideo,
        types.InputMediaAudio,
        types.InputMediaDocument,
        types.InputMediaAnimation,
    ):
        kwargs["file_name"] = "chosen.mp4"
    media = factory(file, **kwargs)
    queries = []

    async def invoke(query):
        # Exercise actual raw serializers; no provider I/O or upload session exists.
        assert isinstance(query, raw.functions.messages.UploadMedia)
        assert query.media.file is file
        assert isinstance(query.write(), bytes)
        queries.append(query)
        if factory is types.InputMediaPhoto:
            return SimpleNamespace(
                photo=SimpleNamespace(id=10, access_hash=11, file_reference=b"p")
            )
        assert query.media.thumb is thumb
        filenames = [
            a for a in query.media.attributes if isinstance(a, raw.types.DocumentAttributeFilename)
        ]
        assert filenames[0].file_name == "chosen.mp4"
        return SimpleNamespace(document=SimpleNamespace(id=10, access_hash=11, file_reference=b"d"))

    client.invoke = invoke
    result = await media.write(client=client)
    assert len(queries) == 1
    assert isinstance(result.write(), bytes)
    assert result.id.id == 10
    # A disconnected Client cannot upload parts: success proves actual save_file passthrough.
    assert client.is_connected is False


@pytest.mark.parametrize("big", [False, True])
def test_uploaded_name_and_mime_use_handle_metadata(big):
    file = uploaded_file(big, name="track.ogg")
    client = pyrogram.Client("handle-metadata", in_memory=True)
    assert utils.get_file_name(file) == "track.ogg"
    assert utils.get_file_name(file, file_name="chosen.opus") == "chosen.opus"
    assert client.guess_mime_type(file) == "audio/ogg"


@pytest.mark.asyncio
async def test_preuploaded_video_cover_keeps_cover_conversion_and_spoiler():
    client = pyrogram.Client("uploaded-cover", in_memory=True)
    file = uploaded_file(name="clip.mp4")
    cover = uploaded_file(name="cover.jpg")
    queries = []

    async def invoke(query):
        queries.append(query)
        assert isinstance(query.write(), bytes)
        if isinstance(query.media, raw.types.InputMediaUploadedPhoto):
            assert query.media.file is cover
            return SimpleNamespace(
                photo=SimpleNamespace(id=20, access_hash=21, file_reference=b"c")
            )
        assert query.media.file is file
        assert query.media.video_cover.id == 20
        assert query.media.spoiler is True
        assert query.media.ttl_seconds == 5
        return SimpleNamespace(document=SimpleNamespace(id=10, access_hash=11, file_reference=b"d"))

    client.invoke = invoke
    result = await types.InputMediaVideo(file, video_cover=cover, has_spoiler=True).write(
        client=client, ttl_seconds=5
    )
    assert len(queries) == 2
    assert result.video_cover.id == 20
    assert result.spoiler is True
    assert result.ttl_seconds == 5


@pytest.mark.asyncio
async def test_preuploaded_conversion_failure_does_not_try_path_or_reupload():
    client = pyrogram.Client("uploaded-failure", in_memory=True)
    file = uploaded_file(name="fixture.jpg")

    async def invoke(query):
        assert query.media.file is file
        raise ValueError("provider conversion failed")

    client.invoke = invoke
    with pytest.raises(ValueError, match="provider conversion failed"):
        await types.InputMediaPhoto(file).write(client=client)

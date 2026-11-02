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

import asyncio
import contextlib
import io
import threading
from hashlib import md5
from typing import TYPE_CHECKING, Final, cast

import pytest

import pyrogram
from pyrogram import StopTransmission, raw, types
from pyrogram.errors import AuthKeyUnregistered, ChatWriteForbidden, FloodPremiumWait, FloodWait
from pyrogram.methods.advanced.save_file import SaveFile

if TYPE_CHECKING:
    from concurrent.futures import Executor
    from pathlib import Path

_PART_SIZE: Final[int] = 512 * 1024

# `rnd_id()` below answers with this too, so a re-upload addresses the same file as the
#  upload that produced it.
_FILE_ID: Final[int] = 1234567890


class Storage:
    async def dc_id(self) -> int:
        return 2


class Media:
    """A media session that records the parts it was asked to save."""

    def __init__(self, rejects_part: int | None = None) -> None:
        self.rejects_part = rejects_part
        self.saved_parts: list[int] = []

    async def invoke(
        self,
        query: raw.functions.upload.SaveFilePart | raw.functions.upload.SaveBigFilePart,
        **kwargs,
    ) -> bool | None:
        if query.file_part == self.rejects_part:
            raise ConnectionError("the server closed the connection")

        self.saved_parts.append(query.file_part)


class Uploader(SaveFile):
    def __init__(self, media: Media) -> None:
        self.media = media
        self.name = "uploader"
        self.me: types.User | None = None
        self.storage = Storage()
        self.executor: Executor | None = None
        self.save_file_semaphore = asyncio.Semaphore(1)
        self.fallback: UploadMedia | None = None
        self.fallback_calls = 0
        self.media_sessions: dict[int, Media] = {}

    async def _get_upload_fallback_session(self, dc_id, failed_session):
        self.fallback_calls += 1
        return self.fallback

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_event_loop()

    def rnd_id(self) -> int:
        return _FILE_ID

    async def get_session(self, dc_id: int, *, is_media: bool = False) -> Media:
        return self.media


def uploader(media: Media) -> pyrogram.Client:
    # `Uploader` subclasses only the mixin, and the `save_file` overloads pin
    #  `self: pyrogram.Client`.
    return cast("pyrogram.Client", Uploader(media))


@pytest.fixture
def three_parts(tmp_path: Path) -> str:
    path = tmp_path / "upload.bin"
    path.write_bytes(b"x" * (2 * _PART_SIZE + 1))

    return str(path)


@pytest.mark.asyncio
async def test_a_finished_upload_describes_every_part(three_parts: str) -> None:
    media = Media()

    file = await uploader(media).save_file(three_parts)

    assert isinstance(file, raw.types.InputFile)
    assert file.parts == 3
    assert file.md5_checksum == md5(b"x" * (2 * _PART_SIZE + 1)).hexdigest()
    assert media.saved_parts == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_finished_big_upload_describes_every_part() -> None:
    media = Media()
    tracked_file = TrackedFile(b"x" * (21 * _PART_SIZE))

    file = await uploader(media).save_file(tracked_file)

    assert isinstance(file, raw.types.InputFileBig)
    assert file.parts == 21
    assert file.id == _FILE_ID
    assert media.saved_parts == list(range(21))
    assert not tracked_file.closed
    tracked_file.close()


@pytest.mark.asyncio
async def test_a_part_the_server_refused_reaches_the_caller(three_parts: str) -> None:
    # The failure used to be logged inside the worker and nowhere else: `save_file()` handed back
    #  an `InputFile` for a file the server never received in full, and the send that followed
    #  failed with an unrelated error (much later still, for a caller that stored the id).
    media = Media(rejects_part=1)

    with pytest.raises(ConnectionError):
        await uploader(media).save_file(three_parts)


@pytest.mark.asyncio
async def test_a_refused_part_stops_the_remaining_parts(three_parts: str) -> None:
    media = Media(rejects_part=1)

    with pytest.raises(ConnectionError):
        await uploader(media).save_file(three_parts)

    # Once a part has failed, sending the remaining parts cannot complete this upload.
    assert media.saved_parts == [0]


@pytest.mark.asyncio
async def test_re_uploading_one_missing_part_answers_with_nothing(three_parts: str) -> None:
    media = Media()

    file = await uploader(media).save_file(
        three_parts,
        file_id=_FILE_ID,
        file_part=1,
    )

    assert file is None
    assert media.saved_parts == [1]


@pytest.mark.asyncio
async def test_a_missing_part_the_server_refused_reaches_the_caller_too(three_parts: str) -> None:
    media = Media(rejects_part=1)

    with pytest.raises(ConnectionError):
        await uploader(media).save_file(
            three_parts,
            file_id=_FILE_ID,
            file_part=1,
        )


@pytest.mark.asyncio
async def test_no_path_is_not_an_upload_at_all() -> None:
    assert await uploader(Media()).save_file(None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file",
    [
        raw.types.InputFile(id=_FILE_ID, parts=1, name="cached.bin", md5_checksum="hash"),
        raw.types.InputFileBig(id=_FILE_ID, parts=1, name="cached.bin"),
    ],
)
async def test_an_existing_input_file_is_returned_without_upload(file) -> None:
    media = Media()

    assert await uploader(media).save_file(file) is file
    assert media.saved_parts == []


class TrackedFile(io.BytesIO):
    name = "upload.bin"


@pytest.mark.asyncio
@pytest.mark.parametrize("workers_count", [1, 4], ids=["small", "big"])
@pytest.mark.parametrize("owns_file", [False, True], ids=["caller-file", "sdk-file"])
async def test_a_part_failure_drains_workers_and_unblocks_the_producer(
    monkeypatch, workers_count: int, *, owns_file: bool
) -> None:
    producer_blocked = asyncio.Event()
    fail_part = asyncio.Event()
    cleanup_started = asyncio.Event()
    finish_cleanup = asyncio.Event()
    failure = ConnectionError("the server closed the connection")
    sent_parts: list[int] = []
    cancelled_parts: list[int] = []
    worker_tasks: list[asyncio.Task] = []

    class ReadingFile(TrackedFile):
        reads = 0

        def read(self, size: int | None = -1) -> bytes:
            self.reads += 1
            # Active RPCs plus one queued part leave the next `put()` blocked.
            if self.reads == workers_count + 2:
                producer_blocked.set()
            return super().read(size)

    class FailingMedia(Media):
        async def invoke(self, query, **kwargs) -> None:
            task = asyncio.current_task()
            assert task is not None
            worker_tasks.append(task)
            sent_parts.append(query.file_part)
            if query.file_part == 0:
                await fail_part.wait()
                raise failure
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_parts.append(query.file_part)
                if len(cancelled_parts) == workers_count - 1:
                    cleanup_started.set()
                await finish_cleanup.wait()
                raise

    tracked_file = ReadingFile(b"x" * ((21 if workers_count == 4 else 5) * _PART_SIZE))
    media = FailingMedia()
    client = uploader(media)
    if owns_file:
        monkeypatch.setattr("builtins.open", lambda *args, **kwargs: tracked_file)
    upload = asyncio.create_task(client.save_file("upload.bin" if owns_file else tracked_file))
    try:
        await asyncio.wait_for(producer_blocked.wait(), timeout=1)
        assert sent_parts == list(range(workers_count))
        fail_part.set()
        if workers_count > 1:
            await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)
            assert not upload.done()
            assert not tracked_file.closed
            assert client.save_file_semaphore.locked()
        finish_cleanup.set()
        with pytest.raises(ConnectionError) as raised:
            await asyncio.wait_for(asyncio.shield(upload), timeout=0.2)
        assert raised.value is failure
        assert sent_parts == list(range(workers_count))
        assert sorted(cancelled_parts) == list(range(1, workers_count))
        assert all(task.done() for task in worker_tasks)
        assert tracked_file.reads == workers_count + 2
        assert tracked_file.closed is owns_file
        assert not client.save_file_semaphore.locked()
    finally:
        fail_part.set()
        finish_cleanup.set()
        if not upload.done():
            upload.cancel()
        await asyncio.gather(upload, return_exceptions=True)
        tracked_file.close()


class FloodingMedia(Media):
    def __init__(self, wait_type: type[FloodWait | FloodPremiumWait] = FloodWait) -> None:
        super().__init__()
        self.wait_type = wait_type
        self.sent_parts: list[int] = []

    async def invoke(
        self,
        query: raw.functions.upload.SaveFilePart | raw.functions.upload.SaveBigFilePart,
        **kwargs,
    ) -> None:
        self.sent_parts.append(query.file_part)

        if len(self.sent_parts) == 1:
            raise self.wait_type(3600)


class BlockingMedia(Media):
    def __init__(self, workers_count: int = 1) -> None:
        super().__init__()
        self.workers_count = workers_count
        self.sent_parts: list[int] = []
        self.workers_started = asyncio.Event()
        self.workers_cancelled = asyncio.Event()
        self._cancelled_parts: list[int] = []
        self.release = asyncio.Event()

    async def invoke(
        self,
        query: raw.functions.upload.SaveFilePart | raw.functions.upload.SaveBigFilePart,
        **kwargs,
    ) -> None:
        self.sent_parts.append(query.file_part)

        if len(self.sent_parts) == self.workers_count:
            self.workers_started.set()

        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self._cancelled_parts.append(query.file_part)

            if len(self._cancelled_parts) == self.workers_count:
                self.workers_cancelled.set()

            raise


async def await_cancelled(task: asyncio.Task[object]) -> None:
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_type", [FloodWait, FloodPremiumWait])
async def test_a_long_flood_wait_retries_when_the_upload_is_not_cancelled(
    monkeypatch, wait_type
) -> None:
    media = FloodingMedia(wait_type)
    tracked_file = TrackedFile(b"x")
    waits: list[int] = []

    async def skip_wait(seconds: int) -> None:
        waits.append(seconds)

    monkeypatch.setattr("pyrogram.methods.advanced.save_file.asyncio.sleep", skip_wait)
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: tracked_file)

    client = Uploader(media)
    file = await cast("pyrogram.Client", client).save_file("upload.bin")

    assert isinstance(file, raw.types.InputFile)
    assert waits == [3600]
    assert media.sent_parts == [0, 0]
    assert client.fallback_calls == 0
    assert tracked_file.closed


@pytest.mark.asyncio
async def test_cancelling_a_long_flood_wait_stops_the_worker_and_closes_the_file(
    monkeypatch,
) -> None:
    media = FloodingMedia()
    tracked_file = TrackedFile(b"x")
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    sleep_cancelled = asyncio.Event()

    async def held_sleep(seconds: int) -> None:
        assert seconds == 3600
        sleep_started.set()

        try:
            await release_sleep.wait()
        except asyncio.CancelledError:
            sleep_cancelled.set()
            raise

    monkeypatch.setattr("pyrogram.methods.advanced.save_file.asyncio.sleep", held_sleep)
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: tracked_file)

    upload = asyncio.create_task(uploader(media).save_file("upload.bin"))

    await sleep_started.wait()
    upload.cancel()

    try:
        await await_cancelled(upload)

        assert media.sent_parts == [0]
        assert sleep_cancelled.is_set()
        assert tracked_file.closed
    finally:
        release_sleep.set()

        if not upload.done():
            with contextlib.suppress(asyncio.CancelledError):
                await upload


@pytest.mark.asyncio
async def test_cancelling_an_active_rpc_keeps_the_caller_file_open() -> None:
    media = BlockingMedia()
    tracked_file = TrackedFile(b"x")

    client = Uploader(media)
    upload = asyncio.create_task(cast("pyrogram.Client", client).save_file(tracked_file))

    await media.workers_started.wait()
    upload.cancel()

    try:
        await await_cancelled(upload)

        assert media.sent_parts == [0]
        assert media.workers_cancelled.is_set()
        assert not tracked_file.closed
        assert client.fallback_calls == 0
    finally:
        media.release.set()

        if not upload.done():
            with contextlib.suppress(asyncio.CancelledError):
                await upload


@pytest.mark.asyncio
async def test_stop_transmission_cancels_every_active_upload_worker(monkeypatch) -> None:
    media = BlockingMedia(workers_count=4)
    tracked_file = TrackedFile(b"x" * (21 * _PART_SIZE))
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: tracked_file)

    async def stop_after_every_worker_is_active(*args: object) -> None:
        await asyncio.sleep(0)

        if media.workers_started.is_set():
            raise StopTransmission

    with pytest.raises(StopTransmission):
        await uploader(media).save_file("upload.bin", progress=stop_after_every_worker_is_active)

    assert len(media.sent_parts) == 4
    assert media.workers_cancelled.is_set()
    assert tracked_file.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asynchronous",
    [
        pytest.param(True, id="async-callback"),
        pytest.param(False, id="sync-callback-via-executor"),
    ],
)
async def test_stop_transmission_from_a_progress_callback_ends_the_upload(
    three_parts: str,
    *,
    asynchronous: bool,
) -> None:
    media = Media()
    calls: list[int] = []

    def cancel_on_second_part(current: int, total: int) -> None:
        calls.append(current)

        if len(calls) == 2:
            raise StopTransmission

    async def asynchronous_cancel(current: int, total: int) -> None:
        cancel_on_second_part(current, total)

    progress = asynchronous_cancel if asynchronous else cancel_on_second_part

    with pytest.raises(StopTransmission):
        await uploader(media).save_file(three_parts, progress=progress)

    # Cancellation also stops a produced part that has not entered its RPC yet.
    # The third part is never produced, and no worker sends after the abort returns.
    assert media.saved_parts in ([0], [0, 1])
    saved_parts = media.saved_parts.copy()
    await asyncio.sleep(0)
    assert media.saved_parts == saved_parts


@pytest.mark.asyncio
@pytest.mark.parametrize("owns_file", [False, True], ids=["caller-file", "sdk-file"])
@pytest.mark.parametrize("cancel", [False, True], ids=["worker-failure", "caller-cancel"])
@pytest.mark.parametrize("repeats", [0, 3], ids=["no-repeat", "repeat-cancel"])
async def test_sync_progress_finishes_before_releasing_upload_ownership(
    monkeypatch, repeats, *, owns_file, cancel
):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    failure = asyncio.Event()
    callback_result = []
    error = ValueError("fixture part rejected")
    fp = TrackedFile(b"x")

    class FailingMedia(Media):
        async def invoke(self, query, **kwargs):
            await failure.wait()
            raise error

    client = Uploader(FailingMedia())
    if owns_file:
        monkeypatch.setattr("builtins.open", lambda *args, **kwargs: fp)

    def progress(*args):
        loop.call_soon_threadsafe(started.set)
        release.wait(2)
        callback_result.append((fp.closed, fp.read(1)))

    upload = asyncio.create_task(
        cast("pyrogram.Client", client).save_file(
            "upload.bin" if owns_file else fp, progress=progress
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if cancel:
            upload.cancel()
        else:
            failure.set()
        for i in range(repeats + 1):
            await asyncio.sleep(0.01)
            assert not upload.done()
            assert not fp.closed
            assert client.save_file_semaphore.locked()
            if i < repeats:
                upload.cancel()
        release.set()
        result = (await asyncio.wait_for(asyncio.gather(upload, return_exceptions=True), 1))[0]
        if cancel or repeats:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result is error
        assert callback_result == [(False, b"")]
        assert fp.closed is owns_file
        assert not client.save_file_semaphore.locked()
    finally:
        release.set()
        failure.set()
        await asyncio.gather(upload, return_exceptions=True)
        fp.close()


class UploadMedia(Media):
    def __init__(self, failure=None, rejects_part=0):
        self.saved_parts = []
        self.failure = failure
        self.rejects_part = rejects_part
        self.requests = []
        self.stops = 0

    async def invoke(self, query, **kwargs):
        self.requests.append((query, kwargs))
        if self.failure is not None and query.file_part == self.rejects_part:
            raise self.failure
        self.saved_parts.append(query.file_part)
        return True

    async def stop(self):
        self.stops += 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_part", [0, 1])
@pytest.mark.parametrize("big", [False, True], ids=["small", "big"])
async def test_transport_failure_retries_identical_part_on_one_owned_session(failed_part, big):
    source = UploadMedia(ConnectionError("fixture transport failure"), failed_part)
    fallback = UploadMedia()
    client = Uploader(source)
    client.fallback = fallback
    fp = TrackedFile(b"x" * ((21 if big else 3) * _PART_SIZE))
    try:
        result = await cast("pyrogram.Client", client).save_file(fp)
        assert result.parts == (21 if big else 3)
        assert result.id == _FILE_ID
        assert client.fallback_calls == 1
        assert fallback.stops == 1
        assert source.stops == 0
        original = next(q for q, _ in source.requests if q.file_part == failed_part)
        retried = next(q for q, _ in fallback.requests if q.file_part == failed_part)
        assert retried is original
        assert retried.write() == original.write()
        assert sorted(source.saved_parts + fallback.saved_parts) == list(range(result.parts))
        for _, options in source.requests + fallback.requests:
            assert options["retries"] == 1
        assert not fp.closed
    finally:
        fp.close()


@pytest.mark.asyncio
async def test_missing_part_fallback_preserves_file_id_and_scope(three_parts):
    source = UploadMedia(TimeoutError("fixture readiness failure"), 1)
    fallback = UploadMedia()
    client = Uploader(source)
    client.fallback = fallback
    assert (
        await cast("pyrogram.Client", client).save_file(three_parts, file_id=_FILE_ID, file_part=1)
        is None
    )
    assert [q.file_part for q, _ in source.requests] == [1]
    assert [q.file_part for q, _ in fallback.requests] == [1]
    assert source.requests[0][0] is fallback.requests[0][0]
    assert fallback.requests[0][0].file_id == _FILE_ID
    assert fallback.stops == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("alternative", [False, True], ids=["no-alternative", "fallback-fails"])
async def test_upload_fallback_exhaustion_is_finite(three_parts, alternative):
    error = ConnectionError("fixture source failure")
    source = UploadMedia(error)
    fallback_error = TimeoutError("fixture fallback failure")
    fallback = UploadMedia(fallback_error)
    client = Uploader(source)
    client.fallback = fallback if alternative else None
    with pytest.raises((ConnectionError, TimeoutError)) as raised:
        await cast("pyrogram.Client", client).save_file(three_parts)
    assert raised.value is (fallback_error if alternative else error)
    assert client.fallback_calls == 1
    assert len(source.requests) == 1
    assert len(fallback.requests) == int(alternative)
    assert fallback.stops == int(alternative)
    assert source.stops == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("file/progress"),
        PermissionError("permission"),
        AuthKeyUnregistered(),
        ChatWriteForbidden(),
    ],
)
async def test_non_transport_failures_never_open_upload_fallback(three_parts, error):
    source = UploadMedia(error)
    client = Uploader(source)
    with pytest.raises(type(error)) as raised:
        await cast("pyrogram.Client", client).save_file(three_parts)
    assert raised.value is error
    assert client.fallback_calls == 0


@pytest.mark.asyncio
async def test_concurrent_worker_failures_share_one_fallback_without_stopping_borrowed_session():
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0

    class ConcurrentMedia(UploadMedia):
        async def invoke(self, query, **kwargs):
            nonlocal count
            self.requests.append((query, kwargs))
            if query.file_id != _FILE_ID:
                await release.wait()
                return True
            count += 1
            if count == 4:
                entered.set()
            await entered.wait()
            raise ConnectionError("fixture shared transport failure")

    source = ConcurrentMedia()
    fallback = UploadMedia()
    client = Uploader(source)
    client.fallback = fallback
    client.media_sessions = {2: source}
    unrelated = asyncio.create_task(
        source.invoke(raw.functions.upload.SaveFilePart(file_id=99, file_part=0, bytes=b"other"))
    )
    fp = TrackedFile(b"x" * (21 * _PART_SIZE))
    try:
        result = await asyncio.wait_for(cast("pyrogram.Client", client).save_file(fp), 1)
        assert result.parts == 21
        assert client.fallback_calls == 1
        assert sorted(fallback.saved_parts) == list(range(21))
        assert client.media_sessions == {2: source}
        assert not unrelated.done()
        assert source.stops == 0
        assert fallback.stops == 1
        release.set()
        assert await unrelated is True
    finally:
        release.set()
        await asyncio.gather(unrelated, return_exceptions=True)
        fp.close()


@pytest.mark.asyncio
async def test_progress_transport_error_is_not_upload_retry_permission(three_parts):
    client = Uploader(Media())
    error = ConnectionError("fixture progress callback failure")

    def progress(*args):
        raise error

    with pytest.raises(ConnectionError) as raised:
        await cast("pyrogram.Client", client).save_file(three_parts, progress=progress)
    assert raised.value is error
    assert client.fallback_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["part", "close"])
async def test_owned_fallback_is_closed_only_after_work_drains_despite_repeat_cancellation(
    monkeypatch, phase
):
    entered = asyncio.Event()
    drain_started = asyncio.Event()
    release_work = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    source = UploadMedia(ConnectionError("fixture source transport failure"))
    fp = TrackedFile(b"x")

    class OwnedMedia(UploadMedia):
        async def invoke(self, query, **kwargs):
            self.requests.append((query, kwargs))
            entered.set()
            if phase == "part":
                try:
                    await asyncio.Event().wait()
                finally:
                    drain_started.set()
                    await release_work.wait()
            return True

        async def stop(self):
            assert phase != "part" or release_work.is_set()
            self.stops += 1
            close_started.set()
            await release_close.wait()

    fallback = OwnedMedia()
    client = Uploader(source)
    client.fallback = fallback
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: fp)
    upload = asyncio.create_task(cast("pyrogram.Client", client).save_file("fixture.bin"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if phase == "part":
            upload.cancel()
            await asyncio.wait_for(drain_started.wait(), 1)
        else:
            await asyncio.wait_for(close_started.wait(), 1)
        for _ in range(3):
            upload.cancel()
            await asyncio.sleep(0.01)
            assert not upload.done()
            assert not fp.closed
            assert client.save_file_semaphore.locked()
            assert source.stops == 0
        if phase == "part":
            assert fallback.stops == 0
            release_work.set()
            await asyncio.wait_for(close_started.wait(), 1)
        release_close.set()
        await await_cancelled(upload)
        assert fp.closed
        assert fallback.stops == 1
        assert client.fallback_calls == 1
        assert not client.save_file_semaphore.locked()
    finally:
        release_work.set()
        release_close.set()
        if not upload.done():
            upload.cancel()
        await asyncio.gather(upload, return_exceptions=True)
        fp.close()


@pytest.mark.asyncio
async def test_failed_owned_close_does_not_allow_another_upload_attempt(three_parts):
    error = ConnectionError("fixture close failure")

    class OwnedMedia(UploadMedia):
        async def stop(self):
            self.stops += 1
            raise error

    source = UploadMedia(ConnectionError("fixture source transport failure"))
    fallback = OwnedMedia()
    client = Uploader(source)
    client.fallback = fallback
    with pytest.raises(ConnectionError) as raised:
        await cast("pyrogram.Client", client).save_file(three_parts)
    assert raised.value is error
    assert fallback.saved_parts == [0, 1, 2]
    assert fallback.stops == 1
    assert source.stops == 0
    assert client.fallback_calls == 1

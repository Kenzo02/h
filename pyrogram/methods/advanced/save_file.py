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
import functools
import inspect
import io
import logging
import math
import os
from hashlib import md5
from typing import TYPE_CHECKING, BinaryIO, overload

import pyrogram
from pyrogram import raw
from pyrogram.errors import FloodPremiumWait, FloodWait
from pyrogram.session.session import SessionCleanupError, _finish_cleanup

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyrogram._typing import PathType

log = logging.getLogger(__name__)


class SaveFile:
    # The three shapes below are what the body already does, so a caller storing the result
    #  into a required `InputFile` field is not asked to handle a `None` the arguments it
    #  passed cannot produce.
    #
    # Each repeats the implementation's `self`: a bare one reads as `SaveFile`, which is
    #  wider than `Client`, and an overload the implementation does not accept is an error.
    @overload
    async def save_file(
        self: pyrogram.Client,
        path: None,
        file_id: int | None = None,
        file_part: int = 0,
        progress: Callable | None = None,
        progress_args: tuple = (),
    ) -> None: ...

    @overload
    async def save_file(
        self: pyrogram.Client,
        path: raw.types.InputFile | raw.types.InputFileBig,
        file_id: int | None = None,
        file_part: int = 0,
        progress: Callable | None = None,
        progress_args: tuple = (),
    ) -> raw.types.InputFile | raw.types.InputFileBig: ...

    @overload
    async def save_file(
        self: pyrogram.Client,
        path: PathType | BinaryIO,
        file_id: int,
        file_part: int = 0,
        progress: Callable | None = None,
        progress_args: tuple = (),
    ) -> None: ...

    @overload
    async def save_file(
        self: pyrogram.Client,
        path: PathType | BinaryIO,
        file_id: None = None,
        file_part: int = 0,
        progress: Callable | None = None,
        progress_args: tuple = (),
    ) -> raw.types.InputFile | raw.types.InputFileBig: ...

    async def save_file(
        self: pyrogram.Client,
        path: PathType | BinaryIO | raw.types.InputFile | raw.types.InputFileBig | None,
        file_id: int | None = None,
        file_part: int = 0,
        progress: Callable | None = None,
        progress_args: tuple = (),
    ) -> raw.types.InputFile | raw.types.InputFileBig | None:
        """Upload a file onto Telegram servers, without actually sending the message to anyone.
        Useful whenever an InputFile type is required.

        .. note::

            This is a utility method intended to be used **only** when working with raw
            :obj:`functions <pyrogram.raw.functions>` (i.e: a Telegram API method you wish to use which is not
            available yet in the Client class as an easy-to-use method).

        .. include:: /_includes/usable-by/users-bots.rst

        Parameters:
            path (``str`` | ``os.PathLike`` | ``BinaryIO``):
                The path of the file you want to upload that exists on your local machine or a binary file-like object
                with its attribute ".name" set for in-memory uploads.

            file_id (``int``, *optional*):
                In case a file part expired, pass the file_id and the file_part to retry uploading that specific chunk.

            file_part (``int``, *optional*):
                In case a file part expired, pass the file_id and the file_part to retry uploading that specific chunk.

            progress (``Callable``, *optional*):
                Pass a callback function to view the file transmission progress.
                The function must take *(current, total)* as positional arguments (look at Other Parameters below for a
                detailed description) and will be called back each time a new file chunk has been successfully
                transmitted.

            progress_args (``tuple``, *optional*):
                Extra custom arguments for the progress callback function.
                You can pass anything you need to be available in the progress callback scope; for example, a Message
                object or a Client instance in order to edit the message with the updated progress status.

        Other Parameters:
            current (``int``):
                The amount of bytes transmitted so far.

            total (``int``):
                The total size of the file.

            *args (``tuple``, *optional*):
                Extra custom arguments as defined in the ``progress_args`` parameter.
                You can either keep ``*args`` or add every single extra argument in your function signature.

        Returns:
            ``InputFile`` | ``None``: On success, the uploaded file is returned in form of an InputFile object. In case
            *path* is None, and in case *file_id* is given so that a single missing part is uploaded instead of the
            whole file, None is returned. A failed upload raises.

        Raises:
            RPCError: In case of a Telegram RPC error, including one that happened while a part was being sent.
        """
        # If the given path is already an InputFile (or InputFileBig) instance, nothing needs to be uploaded.
        # Simply return it so that callers can reuse previously-uploaded files transparently.
        if isinstance(path, (raw.types.InputFile, raw.types.InputFileBig)):
            return path

        async with self.save_file_semaphore:
            if path is None:
                return None

            fallback_session = None
            fallback_attempted = False
            fallback_lock = asyncio.Lock()
            loop = asyncio.get_running_loop()
            last_progress = loop.time()
            flood_wait_until = last_progress
            progress_event = asyncio.Event()
            finished = False

            async def worker(session):
                nonlocal fallback_session, fallback_attempted, last_progress, flood_wait_until
                while not finished:
                    data = await queue.get()

                    if data is None:
                        return

                    async with fallback_lock:
                        part_session = fallback_session or session
                    while not finished:
                        try:
                            # Upload parts are hidden and addressed by `file_id` and part index.
                            # Keep native retries local, without replaying any publication helper.
                            await part_session.invoke(
                                data, retries=1, sleep_threshold=0, retry_delay=0
                            )
                            last_progress = loop.time()
                            progress_event.set()
                            break
                        except (FloodWait, FloodPremiumWait) as e:
                            seconds = e.seconds
                            if seconds is None:
                                raise
                            log.warning(
                                f"[{self.name}] Waiting for {seconds} seconds before continuing "
                                f"(required by {type(data).__name__} in save_file worker)"
                            )
                            flood_wait_until = max(flood_wait_until, loop.time() + seconds)
                            progress_event.set()
                            await asyncio.sleep(seconds)
                        except (ConnectionError, TimeoutError):
                            if part_session is fallback_session:
                                raise
                            async with fallback_lock:
                                if not fallback_attempted:
                                    fallback_attempted = True
                                    fallback_session = await self._get_upload_fallback_session(
                                        dc_id, session
                                    )
                                if fallback_session is None:
                                    raise
                                part_session = fallback_session
                        except Exception as e:
                            log.exception(e)
                            raise

            part_size = 512 * 1024

            if isinstance(path, (str, os.PathLike)):
                fp = open(path, "rb")
                owns_file = True
            elif isinstance(path, io.IOBase):
                fp = path
                owns_file = False
            else:
                raise ValueError(
                    "Invalid file. Expected a file path as string or a binary (not text) file pointer"
                )

            try:
                file_name = getattr(fp, "name", "file.jpg")

                fp.seek(0, os.SEEK_END)
                file_size = fp.tell()
                fp.seek(0)

                if file_size == 0:
                    raise ValueError("File size equals to 0 B")

                if self.me and self.me.is_premium:
                    file_size_limit_mib = 4000
                else:
                    file_size_limit_mib = 2000

                if file_size > file_size_limit_mib * 1024 * 1024:
                    raise ValueError(f"Can't upload files bigger than {file_size_limit_mib} MiB")

                file_total_parts = int(math.ceil(file_size / part_size))
                is_big = file_size > 10 * 1024 * 1024
                workers_count = 4 if is_big else 1
                is_missing_part = file_id is not None
                file_id = file_id or self.rnd_id()
                md5_sum = md5() if not is_big and not is_missing_part else None
                md5_checksum = ""
                dc_id = await self.storage.dc_id()

                session = await self.get_session(dc_id, is_media=True)
                queue = asyncio.Queue(1)
                last_progress = loop.time()
                workers = [asyncio.create_task(worker(session)) for _ in range(workers_count)]
                callback_future = None

                async def produce() -> None:
                    nonlocal file_part, md5_checksum, callback_future

                    fp.seek(part_size * file_part)

                    while not finished:
                        chunk = fp.read(part_size)

                        if not chunk:
                            if md5_sum is not None:
                                md5_checksum = "".join(
                                    [hex(i)[2:].zfill(2) for i in md5_sum.digest()]
                                )
                            break

                        if is_big:
                            rpc = raw.functions.upload.SaveBigFilePart(
                                file_id=file_id,
                                file_part=file_part,
                                file_total_parts=file_total_parts,
                                bytes=chunk,
                            )
                        else:
                            rpc = raw.functions.upload.SaveFilePart(
                                file_id=file_id, file_part=file_part, bytes=chunk
                            )

                        await queue.put(rpc)
                        if finished:
                            return

                        if is_missing_part:
                            break

                        if md5_sum is not None:
                            md5_sum.update(chunk)

                        file_part += 1

                        if progress:
                            func = functools.partial(
                                progress,
                                min(file_part * part_size, file_size),
                                file_size,
                                *progress_args,
                            )

                            if inspect.iscoroutinefunction(progress):
                                await func()
                            else:
                                callback_future = asyncio.get_running_loop().run_in_executor(
                                    self.executor, func
                                )
                                # Cancelling the producer cannot stop a running executor callback.
                                # Retain the future for the upload owner's final drain.
                                await asyncio.shield(callback_future)
                    for _ in workers:
                        await queue.put(None)

                async def watch_progress():
                    while not finished:
                        progress_event.clear()
                        remaining = (
                            max(last_progress, flood_wait_until)
                            + self.UPLOAD_STALL_TIMEOUT
                            - loop.time()
                        )
                        if remaining <= 0:
                            msg = "Upload stalled without acknowledged progress"
                            raise TimeoutError(msg)
                        try:
                            await asyncio.wait_for(progress_event.wait(), timeout=remaining)
                        except asyncio.TimeoutError:
                            # Re-read the deadline if another worker extended a provider wait.
                            continue

                producer = asyncio.create_task(produce())
                tasks = [producer, *workers]
                group = asyncio.gather(*tasks)
                watchdog = asyncio.create_task(watch_progress())
                try:
                    # Observe failures even while `produce()` is blocked on `queue.put()`.
                    # The owner cancels once; repeated cancellation must not interrupt drain.
                    done, _ = await asyncio.wait(
                        (group, watchdog), return_when=asyncio.FIRST_COMPLETED
                    )
                    if group in done:
                        await group
                    else:
                        await watchdog
                finally:
                    # Also retires `wait_for()` if it consumed a concurrent cancellation.
                    finished = True
                    progress_event.set()
                    tasks.append(watchdog)
                    for task in tasks:
                        if not task.done():
                            task.cancel()

                    async def cleanup():
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        await asyncio.gather(group, return_exceptions=True)
                        if callback_future is not None:
                            await asyncio.gather(callback_future, return_exceptions=True)
                        if fallback_session is not None:
                            await fallback_session.stop()
                        for result in results:
                            if isinstance(result, SessionCleanupError):
                                raise result

                    await _finish_cleanup(cleanup(), preserve_failure=True)
            finally:
                if owns_file:
                    fp.close()

            if is_missing_part:
                return None

            if is_big:
                return raw.types.InputFileBig(id=file_id, parts=file_total_parts, name=file_name)

            return raw.types.InputFile(
                id=file_id, parts=file_total_parts, name=file_name, md5_checksum=md5_checksum
            )

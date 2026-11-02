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

"""`stop()` does not wait out the updates watchdog.

The watchdog asks the server for the update state after a quiet spell, and `terminate()`
used to await it. A request in flight, retried on a dead link, held `stop()` for minutes.
A client started with `no_updates` receives no updates, so it has nothing to watch.
"""

from __future__ import annotations as _annotations

import asyncio
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

from pyrogram.session.session import SessionCleanupError

if TYPE_CHECKING:
    from pyrogram import Client


async def test_a_client_without_updates_runs_no_watchdog(offline_client: Client) -> None:
    offline_client.no_updates = True

    await offline_client.start()

    try:
        assert offline_client.updates_watchdog_task is None
    finally:
        await offline_client.stop()


@pytest.mark.parametrize("outcome", ["cancel", "failure", "repeat-cancel"])
async def test_stop_cancels_a_watchdog_request_in_flight(
    offline_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    monkeypatch.setattr(offline_client, "UPDATES_WATCHDOG_INTERVAL", 0)

    await offline_client.start()

    in_flight = asyncio.Event()
    draining = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("watchdog cleanup rejected")

    async def hang(query: Any) -> NoReturn:
        del query

        in_flight.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            if outcome == "failure":
                raise failure
        raise AssertionError("unreachable")

    monkeypatch.setattr(offline_client, "invoke", hang)

    await asyncio.wait_for(in_flight.wait(), timeout=1)
    watchdog = offline_client.updates_watchdog_task
    assert watchdog is not None
    stopping = asyncio.create_task(offline_client.stop())
    try:
        await asyncio.wait_for(draining.wait(), timeout=0.2)
        if outcome == "repeat-cancel":
            for _ in range(3):
                stopping.cancel()
                await asyncio.sleep(0)
        assert not stopping.done()
        assert offline_client.is_initialized
        release.set()
        if outcome == "failure":
            with pytest.raises(SessionCleanupError) as caught:
                await stopping
            assert caught.value.failures == (("watchdog", failure),)
            assert offline_client.updates_watchdog_task is watchdog
        elif outcome == "repeat-cancel":
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert offline_client.updates_watchdog_task is None
        else:
            await stopping
            assert offline_client.updates_watchdog_task is None
        assert watchdog.done()
        assert not offline_client.is_initialized
        assert not offline_client.updates_watchdog_event.is_set()
    finally:
        release.set()
        if not watchdog.done():
            watchdog.cancel()
        await asyncio.gather(stopping, watchdog, return_exceptions=True)

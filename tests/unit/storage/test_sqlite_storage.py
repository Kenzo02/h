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

from pathlib import Path

import pytest

from pyrogram.storage import SQLiteStorage, UpdateState


@pytest.mark.asyncio
async def test_sqlite_storage_round_trips_split_update_states():
    storage = SQLiteStorage("update-state", Path("."), in_memory=True)
    await storage.open()

    try:
        initial = UpdateState(0, 10, 20, 30, 40)
        assert await storage.set_update_state(initial) is None
        assert await storage.set_update_state(UpdateState(0, None, 21, None, 41)) is None
        assert await storage.set_update_state([UpdateState(1, 50, None, 60, None)]) is None

        assert await storage.get_update_states(0) == [UpdateState(0, 10, 21, 30, 41)]
        assert await storage.get_update_states([1]) == [UpdateState(1, 50, None, 60, None)]
        assert await storage.get_update_states([]) == []

        assert await storage.delete_update_state([0, 1]) is None

        assert await storage.get_update_states() == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sqlite_storage_keeps_legacy_update_state_adapter():
    storage = SQLiteStorage("legacy-update-state", Path("."), in_memory=True)
    await storage.open()

    try:
        assert await storage.update_state() == []

        assert await storage.update_state((0, 10, 20, 30, 40)) is None
        assert await storage.update_state((1, 50, None, 10, None)) is None

        assert await storage.update_state() == [
            (1, 50, None, 10, None),
            (0, 10, 20, 30, 40),
        ]

        assert await storage.update_state(1) is None
        assert await storage.update_state() == [(0, 10, 20, 30, 40)]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_conn_property_round_trips_the_connection() -> None:
    # self.conn moved from a plain attribute (declared non-Optional via a
    #  `# type:` comment but assigned None in __init__) to a property backed by
    #  self._conn, so open()/close() and every query still have to see the same
    #  connection object through the ordinary self.conn read/write syntax.
    storage = SQLiteStorage("test", Path(), in_memory=True)

    await storage.open()
    assert await storage.is_bot() is None

    await storage.date(0)
    assert await storage.date() == 0

    await storage.close()


def test_conn_raises_before_open() -> None:
    storage = SQLiteStorage("test", Path(), in_memory=True)

    with pytest.raises(RuntimeError):
        _ = storage.conn

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

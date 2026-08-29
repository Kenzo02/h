from pathlib import Path

import pytest

from pyrogram.storage import SQLiteStorage, UpdateState


@pytest.mark.asyncio
async def test_sqlite_storage_round_trips_split_update_states():
    storage = SQLiteStorage("update-state", Path("."), in_memory=True)
    await storage.open()

    try:
        initial = UpdateState(0, 10, 20, 30, 40)
        await storage.set_update_state(initial)
        await storage.set_update_state(UpdateState(0, None, 21, None, 41))
        await storage.set_update_state([UpdateState(1, 50, None, 60, None)])

        assert await storage.get_update_states(0) == [UpdateState(0, 10, 21, 30, 41)]
        assert await storage.get_update_states([1]) == [UpdateState(1, 50, None, 60, None)]
        assert await storage.get_update_states([]) == []

        await storage.delete_update_state([0, 1])

        assert await storage.get_update_states() == []
    finally:
        await storage.close()

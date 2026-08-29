import asyncio

import pytest

from pyrogram.client import Cache


def test_cache_rejects_non_positive_capacity():
    with pytest.raises(ValueError, match="capacity must be greater than 0"):
        Cache(0)


@pytest.mark.asyncio
async def test_cache_moves_hits_and_evicts_least_recently_used_entry():
    cache = Cache(2)
    await cache.set("old", 1)
    await cache.set("new", 2)

    assert await cache.get("old") == 1

    await cache.set("latest", 3)

    assert await cache.get("old") == 1
    assert await cache.get("latest") == 3
    assert await cache.get("new") is None


@pytest.mark.asyncio
async def test_cache_concurrent_writes_respect_capacity():
    cache = Cache(2)

    await asyncio.gather(*(cache.set(key, key) for key in range(100)))

    present = []
    for key in range(100):
        value = await cache.get(key)
        if value is not None:
            present.append((key, value))

    assert len(present) == 2
    assert all(key == value for key, value in present)

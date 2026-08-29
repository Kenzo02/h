import asyncio

import pytest

from pyrogram.client import Cache, Client


def test_cache_rejects_negative_capacity():
    with pytest.raises(ValueError, match="capacity must be non-negative"):
        Cache(-1)


@pytest.mark.asyncio
async def test_zero_capacity_cache_is_a_no_op():
    cache = Cache(0)

    await cache.set("message", 1)

    assert await cache.get("message") is None
    assert len(cache) == 0
    assert not cache
    assert "capacity=0" in repr(cache)


@pytest.mark.parametrize(
    ("message_capacity", "topic_capacity"),
    [(0, 1), (1, 0), (0, 0)],
)
def test_client_accepts_disabled_cache_capacity(
    tmp_path,
    message_capacity,
    topic_capacity,
):
    client = Client(
        "cache-disabled",
        api_id=1,
        api_hash="hash",
        in_memory=True,
        workdir=tmp_path,
        max_message_cache_size=message_capacity,
        max_topic_cache_size=topic_capacity,
    )

    assert client.message_cache.capacity == message_capacity
    assert client.topic_cache.capacity == topic_capacity


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

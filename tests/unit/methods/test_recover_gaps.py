import asyncio
from types import SimpleNamespace

import pytest

from pyrogram.client import Client as PyrogramClient
from pyrogram import raw
from pyrogram.methods.advanced.recover_gaps import RecoverGaps
from pyrogram.storage import UpdateState


class Storage:
    def __init__(self):
        self.states = [UpdateState(0, 10, 20, 30, 40)]
        self.set_calls = []
        self.saved = False

    async def get_update_states(self, ids=None):
        assert ids is None
        return self.states

    async def set_update_state(self, state):
        self.set_calls.append(state)

        for index, current in enumerate(self.states):
            if current.id == state.id:
                self.states[index] = UpdateState(
                    state.id,
                    state.pts if state.pts is not None else current.pts,
                    state.qts if state.qts is not None else current.qts,
                    state.date if state.date is not None else current.date,
                    state.seq if state.seq is not None else current.seq,
                )
                break
        else:
            self.states.append(state)

    async def save(self):
        self.saved = True


class Client(RecoverGaps):
    def __init__(self):
        self.storage = Storage()
        self.dispatcher = SimpleNamespace(updates_queue=asyncio.Queue())
        self._update_state_lock = asyncio.Lock()
        self.queries = []

    async def fetch_peers(self, peers):
        return False

    async def invoke(self, query):
        self.queries.append(query)
        return raw.types.updates.DifferenceEmpty(date=31, seq=41)


@pytest.mark.asyncio
async def test_recover_gaps_uses_split_state_storage_and_persists_progress():
    client = Client()

    assert await client.recover_gaps() == (0, 0)

    assert len(client.queries) == 1
    assert isinstance(client.queries[0], raw.functions.updates.GetDifference)
    assert client.queries[0].pts == 10
    assert client.queries[0].date == 30
    assert client.storage.set_calls == [
        UpdateState(0, None, None, 31, 41),
    ]
    assert client.storage.saved


class ChannelClient(Client):
    def __init__(self):
        super().__init__()
        self.storage.states = [UpdateState(-1000000000123, 10, 20, 30, 40)]

    async def resolve_peer(self, chat_id):
        return raw.types.InputChannel(channel_id=123, access_hash=456)

    async def invoke(self, query):
        self.queries.append(query)
        return raw.types.updates.ChannelDifferenceEmpty(pts=11, final=True)


@pytest.mark.asyncio
async def test_recover_gaps_persists_channel_empty_pts():
    client = ChannelClient()

    assert await client.recover_gaps() == (0, 0)

    assert isinstance(client.queries[0], raw.functions.updates.GetChannelDifference)
    assert client.queries[0].pts == 10
    assert client.storage.set_calls == [
        UpdateState(-1000000000123, 11, None, None, None),
    ]


class LiveClient(Client):
    handle_updates = PyrogramClient.handle_updates
    _handle_updates = PyrogramClient._handle_updates


class ConcurrentClient(LiveClient):
    def __init__(self):
        super().__init__()
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()

    async def invoke(self, query):
        self.queries.append(query)
        self.recovery_started.set()
        await self.release_recovery.wait()
        return raw.types.updates.DifferenceEmpty(date=31, seq=41)


def live_updates(*, channel_id=None):
    update = SimpleNamespace(pts=99, qts=88)

    if channel_id is not None:
        update.channel_id = channel_id

    return raw.types.Updates(
        updates=[update],
        users=[],
        chats=[],
        date=77,
        seq=66,
    )


@pytest.mark.asyncio
async def test_recover_gaps_orders_difference_empty_before_live_cursor_advance():
    client = ConcurrentClient()
    recovery_task = asyncio.create_task(client.recover_gaps())

    await asyncio.wait_for(client.recovery_started.wait(), timeout=1)
    live_task = asyncio.create_task(client.handle_updates(live_updates()))

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not live_task.done()
        assert client.storage.set_calls == []
    finally:
        client.release_recovery.set()
        await asyncio.gather(recovery_task, live_task)

    assert client.storage.set_calls == [
        UpdateState(0, None, None, 31, 41),
        UpdateState(0, 99, 88, None, None),
        UpdateState(0, None, None, 77, 66),
    ]
    assert client.storage.states == [UpdateState(0, 99, 88, 77, 66)]


class ConcurrentChannelClient(ChannelClient):
    handle_updates = PyrogramClient.handle_updates
    _handle_updates = PyrogramClient._handle_updates

    def __init__(self):
        super().__init__()
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()

    async def invoke(self, query):
        self.queries.append(query)
        self.recovery_started.set()
        await self.release_recovery.wait()
        return raw.types.updates.ChannelDifferenceEmpty(pts=31, final=True)


@pytest.mark.asyncio
async def test_recover_gaps_orders_channel_difference_empty_before_live_cursor_advance():
    client = ConcurrentChannelClient()
    recovery_task = asyncio.create_task(client.recover_gaps())

    await asyncio.wait_for(client.recovery_started.wait(), timeout=1)
    live_task = asyncio.create_task(client.handle_updates(live_updates(channel_id=123)))

    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert not live_task.done()
        assert client.storage.set_calls == []
    finally:
        client.release_recovery.set()
        await asyncio.gather(recovery_task, live_task)

    channel_id = -1000000000123

    assert client.storage.set_calls == [
        UpdateState(channel_id, 31, None, None, None),
        UpdateState(channel_id, 99, 88, None, None),
        UpdateState(0, None, None, 77, 66),
    ]
    assert client.storage.states == [
        UpdateState(channel_id, 99, 88, 30, 40),
        UpdateState(0, None, None, 77, 66),
    ]

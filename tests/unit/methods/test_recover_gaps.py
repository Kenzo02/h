from types import SimpleNamespace

import pytest

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

    async def save(self):
        self.saved = True


class Client(RecoverGaps):
    def __init__(self):
        self.storage = Storage()
        self.dispatcher = SimpleNamespace(updates_queue=None)
        self.queries = []

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
        UpdateState(0, 10, 20, 31, 41),
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
        UpdateState(-1000000000123, 11, 20, 30, 40),
    ]

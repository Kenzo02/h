import pytest

from pyrogram.storage import Storage, UpdateState


class LegacyStorage(Storage):
    def __init__(self):
        self.states = [
            (0, 10, 20, 30, 40),
            (1, 50, 60, 70, 80),
        ]

    async def open(self):
        pass

    async def save(self):
        pass

    async def close(self):
        pass

    async def delete(self):
        pass

    async def update_peers(self, peers):
        pass

    async def update_usernames(self, usernames):
        pass

    async def update_state(self, update_state=object):
        if update_state is object:
            return sorted(self.states, key=lambda state: state[3])

        if isinstance(update_state, int):
            self.states = [state for state in self.states if state[0] != update_state]
            return

        state = tuple(update_state)
        self.states = [current for current in self.states if current[0] != state[0]]
        self.states.append(state)

    async def get_peer_by_id(self, peer_id):
        return None

    async def get_peer_by_username(self, username):
        return None

    async def get_peer_by_phone_number(self, phone_number):
        return None

    async def dc_id(self, value=object):
        return 0

    async def api_id(self, value=object):
        return 0

    async def server_address(self, value=object):
        return ""

    async def port(self, value=object):
        return 0

    async def test_mode(self, value=object):
        return False

    async def auth_key(self, value=object):
        return b""

    async def date(self, value=object):
        return 0

    async def user_id(self, value=object):
        return 0

    async def is_bot(self, value=object):
        return False


class StorageWithoutUpdateState(LegacyStorage):
    update_state = Storage.update_state


class RecursiveLegacyStorage(LegacyStorage):
    async def update_state(self, update_state=object):
        return await Storage.update_state(self, update_state)


@pytest.mark.asyncio
async def test_legacy_storage_implements_split_api_with_partial_merges():
    storage = LegacyStorage()

    assert await storage.get_update_states() == [
        UpdateState(0, 10, 20, 30, 40),
        UpdateState(1, 50, 60, 70, 80),
    ]
    assert await storage.get_update_states(0) == [UpdateState(0, 10, 20, 30, 40)]
    assert await storage.get_update_states([1]) == [UpdateState(1, 50, 60, 70, 80)]
    assert await storage.get_update_states([]) == []

    assert await storage.set_update_state(UpdateState(0, None, 21, None, 41)) is None
    assert await storage.get_update_states(0) == [UpdateState(0, 10, 21, 30, 41)]

    assert await storage.set_update_state(
        [UpdateState(0, 11, None, None, None), UpdateState(2, None, 22, 32, None)]
    ) is None
    assert await storage.get_update_states([0, 2]) == [
        UpdateState(0, 11, 21, 30, 41),
        UpdateState(2, None, 22, 32, None),
    ]

    assert await storage.delete_update_state([0, 2]) is None
    assert await storage.get_update_states() == [UpdateState(1, 50, 60, 70, 80)]


def test_storage_without_either_update_state_api_remains_abstract():
    with pytest.raises(TypeError, match="abstract"):
        StorageWithoutUpdateState()


@pytest.mark.asyncio
async def test_legacy_bridge_rejects_recursive_update_state_delegation():
    with pytest.raises(RuntimeError, match="Recursive update-state compatibility bridge"):
        await RecursiveLegacyStorage().get_update_states()

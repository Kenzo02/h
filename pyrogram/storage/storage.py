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

import base64
import struct
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyrogram import raw


@dataclass(frozen=True)
class UpdateState:
    id: int
    pts: int | None
    qts: int | None
    date: int | None
    seq: int | None


_UPDATE_STATE_BRIDGE_STACK = ContextVar("storage_update_state_bridge_stack", default=())
_LegacyUpdateState = tuple[int, int | None, int | None, int | None, int | None]


class Storage(ABC):
    """Abstract class for storage engines."""

    _UPDATE_STATE_SPLIT_METHODS = (
        "get_update_states",
        "set_update_state",
        "delete_update_state",
    )

    @abstractmethod
    def _update_state_api(self):
        """Keep subclasses abstract until one update-state API is implemented."""
        raise NotImplementedError

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        has_legacy_api = cls.update_state is not Storage.update_state
        has_split_api = all(
            getattr(cls, method_name) is not Storage.__dict__[method_name]
            for method_name in Storage._UPDATE_STATE_SPLIT_METHODS
        )

        if has_legacy_api or has_split_api:
            cls._update_state_api = None
        else:
            cls._update_state_api = Storage._update_state_api

    OLD_SESSION_STRING_FORMAT = ">B?256sI?"
    OLD_SESSION_STRING_FORMAT_64 = ">B?256sQ?"
    SESSION_STRING_SIZE = 351
    SESSION_STRING_SIZE_64 = 356

    SESSION_STRING_FORMAT = ">BI?256sQ?"

    @abstractmethod
    async def open(self):
        """Opens the storage engine."""
        raise NotImplementedError

    @abstractmethod
    async def save(self):
        """Saves the current state of the storage engine."""
        raise NotImplementedError

    @abstractmethod
    async def close(self):
        """Closes the storage engine."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self):
        """Deletes the storage file."""
        raise NotImplementedError

    @abstractmethod
    async def update_peers(self, peers: Iterable[tuple[int, int, str, str | None]]):
        """
        Update the peers table with the provided information.

        Parameters:
            peers (List of ``tuple[int, int, str, str | None]``):
                A list of tuples containing the
                information of the peers to be updated.
                Each tuple must contain the following information:
                - ``int``: The peer id.
                - ``int``: The peer access hash.
                - ``str``: The peer type ("user", "bot", "group", "direct", "channel", "forum", "supergroup" or "community").
                - ``str`` | ``None``: The peer phone number (if any).
        """
        raise NotImplementedError

    @abstractmethod
    async def update_usernames(self, usernames: Iterable[tuple[int, list[str | None]]]):
        """
        Update the usernames table with the provided information.

        Parameters:
            usernames (List of ``tuple[int, list[str | None]]``):
                A list of tuples containing the
                information of the usernames to be updated. Each tuple must contain the following
                information:
                - ``int``: The peer id.
                - List of ``str`` | ``None``: The peer username (if any).
        """
        raise NotImplementedError

    async def get_update_states(self, ids: int | Iterable[int] | None = None) -> list[UpdateState]:
        """Get the update state of the current session.

        Parameters:
            ids (``int`` | Iterable of ``int``, *optional*):
                Limit the result to the specified state IDs.
                If omitted, all states are returned.

        Returns:
            List of ``UpdateState``: On success, a list of update states is returned.
        """
        if ids is None:
            state_ids = None
        else:
            state_ids = (ids,) if isinstance(ids, int) else tuple(ids)

            if not state_ids:
                return []

        states = [
            self._coerce_update_state(state) for state in await self._call_legacy_update_state()
        ]

        if state_ids is None:
            return states

        return [state for state in states if state.id in state_ids]

    async def set_update_state(self, update_state: UpdateState | Iterable[UpdateState]):
        """Set the update state of the current session.

        Parameters:
            update_state (``UpdateState`` | Iterable of ``UpdateState``):
                The update state or states to set.
        """
        states = [update_state] if isinstance(update_state, UpdateState) else update_state

        for update in states:
            current = await self.get_update_states(update.id)
            state_to_store = update

            if current:
                state_to_store = self._merge_update_state(current[0], update)

            await self._call_legacy_update_state(
                (
                    state_to_store.id,
                    state_to_store.pts,
                    state_to_store.qts,
                    state_to_store.date,
                    state_to_store.seq,
                )
            )

    async def delete_update_state(self, state_id: int | Iterable[int]):
        """Delete the update state of the current session.

        Parameters:
            state_id (``int`` | List of ``int``):
                The id of the update state to delete.
        """
        state_ids = (state_id,) if isinstance(state_id, int) else tuple(state_id)

        for state_id in state_ids:
            await self._call_legacy_update_state(state_id)

    @staticmethod
    def _coerce_update_state(state) -> UpdateState:
        return state if isinstance(state, UpdateState) else UpdateState(*state)

    @staticmethod
    def _merge_update_state(current: UpdateState, update: UpdateState) -> UpdateState:
        return UpdateState(
            update.id,
            update.pts if update.pts is not None else current.pts,
            update.qts if update.qts is not None else current.qts,
            update.date if update.date is not None else current.date,
            update.seq if update.seq is not None else current.seq,
        )

    async def _call_legacy_update_state(
        self, update_state: int | _LegacyUpdateState | type[object] = object
    ):
        update_state_method = type(self).update_state

        if update_state_method is Storage.update_state:
            raise NotImplementedError("No legacy update_state implementation found")

        bridge_stack = _UPDATE_STATE_BRIDGE_STACK.get()
        storage_id = id(self)

        if storage_id in bridge_stack:
            raise RuntimeError("Recursive update-state compatibility bridge")

        token = _UPDATE_STATE_BRIDGE_STACK.set(bridge_stack + (storage_id,))

        try:
            update_state_method = self.update_state

            if update_state is object:
                return await update_state_method()

            return await update_state_method(update_state)
        finally:
            _UPDATE_STATE_BRIDGE_STACK.reset(token)

    async def update_state(self, update_state: int | _LegacyUpdateState | type[object] = object):
        """Compatibility adapter for the pre-split update-state API."""
        if update_state is object:
            return [
                (state.id, state.pts, state.qts, state.date, state.seq)
                for state in await self.get_update_states()
            ]

        if isinstance(update_state, int):
            return await self.delete_update_state(update_state)

        return await self.set_update_state(UpdateState(*cast("_LegacyUpdateState", update_state)))

    @abstractmethod
    async def get_peer_by_id(self, peer_id: int) -> raw.base.InputPeer | None:
        """Retrieve a peer by its ID.

        Parameters:
            peer_id (``int``):
                The ID of the peer to retrieve.

        Returns:
            :obj:`~pyrogram.raw.base.InputPeer` | ``None``: On success, the resolved peer is returned.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_peer_by_username(self, username: str) -> raw.base.InputPeer | None:
        """Retrieve a peer by its username.

        Parameters:
            username (``str``):
                The username of the peer to retrieve.

        Returns:
            :obj:`~pyrogram.raw.base.InputPeer` | ``None``: On success, the resolved peer is returned.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_peer_by_phone_number(self, phone_number: str) -> raw.base.InputPeer | None:
        """Retrieve a peer by its phone number.

        Parameters:
            phone_number (``str``):
                The phone number of the peer to retrieve.

        Returns:
            :obj:`~pyrogram.raw.base.InputPeer` | ``None``: On success, the resolved peer is returned.
        """
        raise NotImplementedError

    # `object` (the class itself) is the not-specified sentinel on every accessor
    #  below: `None` is a real value both stored (`user_id(None)` on logout) and
    #  returned (a fresh session has no row yet), so it cannot mean "read".
    @abstractmethod
    async def dc_id(self, value: int | None | type[object] = object) -> int | None:
        """Get or set the DC ID of the current session.

        Parameters:
            value (``int``, *optional*):
                The DC ID to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def api_id(self, value: int | None | type[object] = object) -> int | None:
        """Get or set the API ID of the current session.

        Parameters:
            value (``int``, *optional*):
                The API ID to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def server_address(self, value: str | None | type[object] = object) -> str | None:
        """Get or set the server address of the current session.

        Parameters:
            value (``str``, *optional*):
                The server address to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def port(self, value: int | None | type[object] = object) -> int | None:
        """Get or set the server port of the current session.

        Parameters:
            value (``int``, *optional*):
                The server port to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def test_mode(self, value: bool | None | type[object] = object) -> bool | None:
        """Get or set the test mode of the current session.

        Parameters:
            value (``bool``, *optional*):
                The test mode to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def auth_key(self, value: bytes | None | type[object] = object) -> bytes | None:
        """Get or set the authorization key of the current session.

        Parameters:
            value (``bytes``, *optional*):
                The authorization key to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def date(self, value: int | None | type[object] = object) -> int | None:
        """Get or set the date of the current session.

        Parameters:
            value (``int``, *optional*):
                The date to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def user_id(self, value: int | None | type[object] = object) -> int | None:
        """Get or set the user ID of the current session.

        Parameters:
            value (``int``, *optional*):
                The user ID to set.
        """
        raise NotImplementedError

    @abstractmethod
    async def is_bot(self, value: bool | None | type[object] = object) -> bool | None:
        """Get or set the bot flag of the current session.

        Parameters:
            value (``bool``, *optional*):
                The bot flag to set.
        """
        raise NotImplementedError

    async def export_session_string(self) -> str:
        """Exports the session string for the current session.

        Returns:
            ``str``: The session string for the current session.
        """
        packed = struct.pack(
            self.SESSION_STRING_FORMAT,
            await self.dc_id(),
            await self.api_id(),
            await self.test_mode(),
            await self.auth_key(),
            await self.user_id(),
            await self.is_bot(),
        )

        return base64.urlsafe_b64encode(packed).decode().rstrip("=")

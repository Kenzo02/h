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

"""Reuse full peer snapshots before hydrating minimal live updates."""

from __future__ import annotations as _annotations

from copy import copy
import time
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pyrogram import raw
from pyrogram.utils.cache import Cache

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pyrogram import Client

# Raw peer metadata only, never messages, updates or tasks. Eviction falls back to
# the existing hydration path rather than retaining an unbounded peer dictionary.
UPDATE_PEER_CACHE_SIZE = 4096
UPDATE_PEER_CACHE_TTL = 600
_Peer = TypeVar("_Peer", bound=raw.base.User | raw.base.Chat)


class UpdatePeerCache(Cache):
    """Bounded snapshots with a non-sliding lifetime and authorization fence."""

    def __init__(self) -> None:
        super().__init__(UPDATE_PEER_CACHE_SIZE)
        self._auth_key: bytes | None = None
        self.generation = 0

    def clear(self) -> None:
        # All cache critical sections run on the client loop without yielding.
        self._cache.clear()
        self._auth_key = None
        self.generation += 1

    def bind(self, auth_key: bytes) -> None:
        if self._auth_key != auth_key:
            self.clear()
            self._auth_key = auth_key

    async def get(self, key: Any, default: Any = None) -> Any:
        async with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return default
            expires, value = entry
            if time.monotonic() >= expires:
                del self._cache[key]
                return default
            self._cache.move_to_end(key)
            return value

    async def set(self, key: Any, value: Any) -> None:
        async with self._lock:
            now = time.monotonic()
            previous = self._cache.get(key)
            # Merged minimal peers pass through `fetch_peers` too. Repeated
            # traffic must not keep a missed invalidation alive indefinitely.
            expires = (
                previous[0]
                if previous is not None and previous[0] > now
                else now + UPDATE_PEER_CACHE_TTL
            )
            self._cache[key] = (expires, value)
            self._cache.move_to_end(key)
            if len(self._cache) > self.capacity:
                self._cache.popitem(last=False)


def peer_cache_for(client: Client) -> UpdatePeerCache | None:
    cache = getattr(client, "_update_peer_cache", None)
    if not isinstance(cache, UpdatePeerCache):
        return None
    auth_key = getattr(getattr(client, "session", None), "auth_key", None)
    if isinstance(auth_key, bytes):
        cache.bind(auth_key)
    return cache


async def ingest_peer_response(client: Client, response: Any) -> None:
    """Ingest native RPC peers and metadata before any consumer dispatches them."""
    cache = peer_cache_for(client)
    if isinstance(response, raw.types.auth.LoginTokenSuccess):
        response = response.authorization
    if cache is not None and isinstance(response, raw.types.auth.Authorization):
        cache.clear()

    # This shared boundary covers short-message expansion and both global and
    # channel gap recovery, not just live `Updates` containers.
    for field in ("updates", "other_updates"):
        updates = getattr(response, field, ())
        if isinstance(updates, (list, tuple)):
            for update in updates:
                await forget_changed_peer(cache, update)
        elif isinstance(updates, raw.base.Updates):
            await ingest_peer_response(client, updates)
    update = getattr(response, "update", None)
    if isinstance(update, raw.base.Update):
        await forget_changed_peer(cache, update)
    if isinstance(response, raw.base.Update):
        await forget_changed_peer(cache, response)

    # Invalidate first; a full peer bundled with the change is already fresh.
    await client.fetch_peers(getattr(response, "users", []))
    await client.fetch_peers(getattr(response, "chats", []))
    peers = response if isinstance(response, (list, tuple)) else [response]
    direct_peers: list[raw.base.User | raw.base.Chat] = [
        cast("raw.base.User | raw.base.Chat", peer)
        for peer in peers
        if isinstance(peer, (raw.base.User, raw.base.Chat))
    ]
    user = getattr(response, "user", None)
    if isinstance(user, raw.base.User):
        direct_peers.append(user)
    if direct_peers:
        await client.fetch_peers(direct_peers)


# https://core.telegram.org/constructor/user specifies these min-field exceptions.
_USER_PRESERVE = frozenset(
    {
        "id",
        "min",
        "access_hash",
        "contact",
        "mutual_contact",
        "attach_menu_enabled",
        "bot_can_edit",
        "close_friend",
        "stories_hidden",
        "first_name",
        "last_name",
        "username",
        "phone",
        "usernames",
        "stories_max_id",
        "photo",
        "status",
    }
)

# A minimal channel may update only the fields explicitly listed by Telegram.
# https://core.telegram.org/constructor/channel
_CHANNEL_APPLY = frozenset(
    {
        "title",
        "megagroup",
        "color",
        "photo",
        "username",
        "usernames",
        "has_geo",
        "noforwards",
        "emoji_status",
        "has_link",
        "slowmode_enabled",
        "scam",
        "fake",
        "gigagroup",
        "forum",
        "level",
        "restricted",
        "restriction_reason",
        "join_to_send",
        "join_request",
        "verified",
        "default_banned_rights",
        "signature_profiles",
        "autotranslation",
        "broadcast_messages_allowed",
        "monoforum",
        "forum_tabs",
        "linked_monoforum_id",
        "send_paid_messages_stars",
        "bot_verification_icon",
    }
)


def _key(peer: raw.base.User | raw.base.Chat) -> tuple[str, int] | None:
    if isinstance(peer, (raw.types.User, raw.types.UserEmpty)):
        return "user", peer.id
    if isinstance(peer, (raw.types.Channel, raw.types.ChannelForbidden)):
        return "channel", peer.id
    return None


async def remember_peer(cache: Cache | None, peer: raw.base.User | raw.base.Chat) -> None:
    if cache is None or getattr(peer, "min", False):
        return
    key = _key(peer)
    if key is None:
        return
    # Empty/forbidden replacements must not leave an older full snapshot usable.
    value = copy(peer) if isinstance(peer, (raw.types.User, raw.types.Channel)) else None
    await cache.set(key, value)


async def forget_changed_peer(cache: Cache | None, update: raw.base.Update) -> None:
    """Metadata-only updates invalidate snapshots until a full peer arrives again."""
    if cache is None:
        return
    if isinstance(
        update, (raw.types.UpdateUserName, raw.types.UpdateUserPhone, raw.types.UpdateUserStatus)
    ):
        await cache.set(("user", update.user_id), None)
    elif isinstance(update, raw.types.UpdateChannel):
        await cache.set(("channel", update.channel_id), None)


def _merge_channel(full: raw.types.Channel, minimal: raw.types.Channel) -> raw.types.Channel:
    merged = copy(full)
    for field in _CHANNEL_APPLY:
        if hasattr(minimal, field):
            setattr(merged, field, getattr(minimal, field))
    return merged


def _merge_user(full: raw.types.User, minimal: raw.types.User) -> raw.types.User:
    merged = copy(full)
    for field in minimal.__slots__:
        if field not in _USER_PRESERVE:
            setattr(merged, field, getattr(minimal, field))
    if minimal.apply_min_photo:
        merged.photo = minimal.photo
    if isinstance(full.status, raw.types.UserStatusEmpty) or full.status is None:
        merged.status = minimal.status
    # A present empty phone marks a non-min access hash. Never replace a full
    # hash with a photo-only min hash, or persist one as an ordinary input peer.
    if minimal.phone == "" and minimal.access_hash is not None:
        merged.access_hash = minimal.access_hash
    return merged


async def cached_update_peers(
    cache: Cache | None,
    peers: Sequence[_Peer],
) -> list[_Peer]:
    """Merge known full peers; leave unresolved peers minimal for native hydration."""
    result = []
    for peer in peers:
        merged = peer
        if cache is not None and isinstance(peer, (raw.types.User, raw.types.Channel)) and peer.min:
            full = await cache.get(_key(peer))
            if (
                isinstance(peer, raw.types.User)
                and isinstance(full, raw.types.User)
                and not full.min
            ):
                merged = cast("_Peer", _merge_user(full, peer))
            elif (
                isinstance(peer, raw.types.Channel)
                and isinstance(full, raw.types.Channel)
                and not full.min
            ):
                merged = cast("_Peer", _merge_channel(full, peer))
        result.append(merged)
    return result

"""Duplicate identities owned by accepted native tasks, not a transport window.

Only proven, positive pts ranges are comparable. A stored last-written cursor
is not a contiguous receipt ledger: do not suppress an unseen lower revision.
Pending IDs/ranges never expire or compete with the bounded completed receipts.
Completed exact ranges cover late fresh-envelope retransmissions without using
the last-written pts cursor as proof that unseen gaps were already delivered.
"""

from __future__ import annotations as _annotations

from copy import copy
from collections import OrderedDict
from typing import Any

from pyrogram import raw

PtsRange = tuple[int, int, int]


def pts_range(update: Any) -> PtsRange | None:
    pts, count = getattr(update, "pts", None), getattr(update, "pts_count", None)
    if type(pts) is not int or type(count) is not int or not 0 < count <= pts:
        return None
    channel = getattr(
        getattr(getattr(update, "message", None), "peer_id", None), "channel_id", None
    )
    channel = channel or getattr(update, "channel_id", None) or 0
    if type(channel) is not int or channel < 0:
        return None
    # Channel sequences are independent; private/basic-group events share box 0.
    return channel, pts - count, pts


class PendingUpdateCustody:
    COMPLETED_LIMIT = 4096

    def __init__(self) -> None:
        self.messages: dict[int, tuple[PtsRange, ...]] = {}
        self.ranges: set[PtsRange] = set()
        self.completed: OrderedDict[PtsRange, None] = OrderedDict()

    def prepare(self, body: Any) -> tuple[Any, tuple[PtsRange, ...]]:
        if isinstance(body, (raw.types.Updates, raw.types.UpdatesCombined)):
            accepted, keys = [], {}
            for update in body.updates:
                key = pts_range(update)
                if key is not None:
                    if key in self.ranges or key in self.completed or key in keys:
                        continue
                    keys[key] = None
                accepted.append(update)
            if len(accepted) != len(body.updates):
                # Keep metadata ingestion and the exact surviving raw objects.
                # Native TTMT evidence snapshots these objects, not copies of them.
                body = copy(body)
                body.updates = accepted
            return body, tuple(keys)
        update = body.update if isinstance(body, raw.types.UpdateShort) else body
        key = pts_range(update)
        if key is not None and (key in self.ranges or key in self.completed):
            return None, ()
        return body, (key,) if key is not None else ()

    def claim(self, message_id: int, keys: tuple[PtsRange, ...]) -> None:
        self.messages[message_id] = keys
        self.ranges.update(keys)

    def release(self, message_id: int, *, completed: bool) -> None:
        keys = self.messages.pop(message_id, ())
        self.ranges.difference_update(keys)
        if completed:
            for key in keys:
                self.completed[key] = None
                self.completed.move_to_end(key)
            while len(self.completed) > self.COMPLETED_LIMIT:
                self.completed.popitem(last=False)

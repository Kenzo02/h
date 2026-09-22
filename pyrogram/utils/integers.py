#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.

from __future__ import annotations as _annotations


def normalize_int64(value: int | None) -> int | None:
    """Convert an unsigned 64-bit value to the signed MTProto representation."""
    if value is None:
        return None

    value = int(value)

    if value >= 1 << 63:
        return value - (1 << 64)

    return value

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

from datetime import datetime
from typing import Optional

from pyrogram import raw, types, utils, enums

from ..object import Object


class SuggestedPostInfo(Object):
    """Contains information about a suggested post.

    If the post can be approved or declined, then changes to the post can be also suggested.
    Use :meth:`~pyrogram.Client.send_message` with reply to the message and suggested post information to suggest message changes.

    Parameters:
        price (:obj:`~pyrogram.types.SuggestedPostPrice`, *optional*):
            Price of the suggested post.

        send_date (:py:obj:`~datetime.datetime`, *optional*):
            Point in time when the post will be published.

        state (:obj:`~pyrogram.enums.SuggestedPostState`, *optional*):
            State of the post.
    """
    def __init__(
        self, *,
        price: Optional["types.SuggestedPostPrice"] = None,
        send_date: Optional[datetime] = None,
        state: Optional["enums.SuggestedPostState"] = None
    ):
        super().__init__()

        self.price = price
        self.send_date = send_date
        self.state = state

    @staticmethod
    def _parse(suggested_post: "raw.types.SuggestedPost") -> Optional["SuggestedPostInfo"]:
        if not suggested_post:
            return None

        state = None

        if suggested_post.accepted:
            state = enums.SuggestedPostState.APPROVED
        elif suggested_post.declined:
            state = enums.SuggestedPostState.DECLINED
        else:
            state = enums.SuggestedPostState.PENDING

        return SuggestedPostInfo(
            price=types.SuggestedPostPrice._parse(suggested_post.price),
            send_date=utils.timestamp_to_datetime(suggested_post.schedule_date),
            state=state,
        )

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

from pyrogram import raw, types, utils

from ..object import Object


class InputSuggestedPostInfo(Object):
    """Contains information about a post to suggest.

    Parameters:
        price (:obj:`~pyrogram.types.SuggestedPostPrice`, *optional*):
            Price of the suggested post.

        send_date (:py:obj:`~datetime.datetime`, *optional*):
            Point in time when the post is expected to be published.
    """
    def __init__(
        self, *,
        price: Optional["types.SuggestedPostPrice"] = None,
        send_date: Optional[datetime] = None
    ):
        super().__init__()

        self.price = price
        self.send_date = send_date

    def write(self) -> "raw.types.InputSuggestedPostInfo":
        return raw.types.SuggestedPost(
            price=self.price.write() if self.price else None,
            schedule_date=utils.datetime_to_timestamp(self.send_date)
        )

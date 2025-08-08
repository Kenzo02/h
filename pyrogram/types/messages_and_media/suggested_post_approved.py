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
from pyrogram import raw, types, utils

from ..object import Object


class SuggestedPostApproved(Object):
    """A suggested post was approved.

    Parameters:
        suggested_post_message_id (``int``, *optional*):
            Identifier of the message with the suggested post.

        price (:obj:`~pyrogram.types.SuggestedPostPrice`, *optional*):
            Price of the suggested post.

        send_date (:py:obj:`~datetime.datetime`, *optional*):
            Point in time when the post is expected to be published.
    """
    def __init__(
        self, *,
        suggested_post_message_id: int = None,
        price: "types.SuggestedPostPrice" = None,
        send_date: datetime = None
    ):
        super().__init__()

        self.suggested_post_message_id = suggested_post_message_id
        self.price = price
        self.send_date = send_date

    @staticmethod
    def _parse(action: "raw.types.MessageActionSuggestedPostApproval", reply_to: "raw.base.MessageReplyHeader") -> "SuggestedPostApproved":
        if not isinstance(action, raw.types.MessageActionSuggestedPostApproval):
            return None

        suggested_post_message_id = None

        if isinstance(reply_to, raw.types.MessageReplyHeader):
            suggested_post_message_id = reply_to.reply_to_msg_id

        return SuggestedPostApproved(
            suggested_post_message_id=suggested_post_message_id,
            price=types.SuggestedPostPrice._parse(action.price),
            send_date=utils.timestamp_to_datetime(action.schedule_date)
        )

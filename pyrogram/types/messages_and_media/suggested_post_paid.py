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

from pyrogram import raw, types

from ..object import Object


class SuggestedPostPaid(Object):
    """A suggested post was published and payment for the post was received.

    Parameters:
        suggested_post_message_id (``int``, *optional*):
            Identifier of the message with the suggested post.

        star_amount (:obj:`~pyrogram.types.StarAmount`, *optional*):
            The amount of received Telegram Stars.

        ton_amount (``int``, *optional*):
            The amount of received Toncoins in the smallest units of the cryptocurrency.
    """
    def __init__(
        self, *,
        suggested_post_message_id: int = None,
        star_amount: "types.StarAmount" = None,
        ton_amount: int = None
    ):
        super().__init__()

        self.suggested_post_message_id = suggested_post_message_id
        self.star_amount = star_amount
        self.ton_amount = ton_amount

    @staticmethod
    def _parse(action: "raw.types.MessageActionSuggestedPostSuccess", reply_to: "raw.base.MessageReplyHeader") -> "SuggestedPostPaid":
        if not isinstance(action, raw.types.MessageActionSuggestedPostSuccess):
            return None

        suggested_post_message_id = None
        star_amount = None
        ton_amount = None

        if isinstance(reply_to, raw.types.MessageReplyHeader):
            suggested_post_message_id = reply_to.reply_to_msg_id

        if isinstance(action.price, raw.types.StarsAmount):
            star_amount = types.StarAmount._parse(action.price)
        elif isinstance(action.price, raw.types.StarsTonAmount):
            ton_amount = action.price.ton_amount


        return SuggestedPostPaid(
            suggested_post_message_id=suggested_post_message_id,
            star_amount=star_amount,
            ton_amount=ton_amount,
        )

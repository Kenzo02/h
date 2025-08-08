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


class SuggestedPostApprovalFailed(Object):
    """Approval of suggested post has failed, because the user which proposed the post had no enough funds.

    Parameters:
        suggested_post_message_id (``int``, *optional*):
            Identifier of the message with the suggested post.

        price (:obj:`~pyrogram.types.SuggestedPostPrice`, *optional*):
            Price of the suggested post.
    """
    def __init__(
        self, *,
        suggested_post_message_id: int = None,
        price: "types.SuggestedPostPrice" = None
    ):
        super().__init__()

        self.suggested_post_message_id = suggested_post_message_id
        self.price = price

    @staticmethod
    def _parse(action: "raw.types.MessageActionSuggestedPostApproval", reply_to: "raw.base.MessageReplyHeader") -> "SuggestedPostApprovalFailed":
        if not isinstance(action, raw.types.MessageActionSuggestedPostApproval):
            return None

        suggested_post_message_id = None

        if isinstance(reply_to, raw.types.MessageReplyHeader):
            suggested_post_message_id = reply_to.reply_to_msg_id

        return SuggestedPostApprovalFailed(
            suggested_post_message_id=suggested_post_message_id,
            price=types.SuggestedPostPrice._parse(action.price),
        )

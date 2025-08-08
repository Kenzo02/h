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

from pyrogram import raw, enums

from ..object import Object


class SuggestedPostRefunded(Object):
    """A suggested post was published and payment for the post was received.

    Parameters:
        suggested_post_message_id (``int``, *optional*):
            Identifier of the message with the suggested post.

        reason (:obj:`~pyrogram.enums.SuggestedPostRefundReason`, *optional*):
            Reason of the refund.
    """
    def __init__(
        self, *,
        suggested_post_message_id: int = None,
        reason: "enums.SuggestedPostRefundReason" = None
    ):
        super().__init__()

        self.suggested_post_message_id = suggested_post_message_id
        self.reason = reason

    @staticmethod
    def _parse(action: "raw.types.MessageActionSuggestedPostRefund", reply_to: "raw.base.MessageReplyHeader") -> "SuggestedPostRefunded":
        if not isinstance(action, raw.types.MessageActionSuggestedPostRefund):
            return None

        suggested_post_message_id = None
        reason = None

        if isinstance(reply_to, raw.types.MessageReplyHeader):
            suggested_post_message_id = reply_to.reply_to_msg_id

        if not reply_to:
            reason = enums.SuggestedPostRefundReason.POST_DELETED
        else:
            reason = enums.SuggestedPostRefundReason.PAYMENT_REFUNDED


        return SuggestedPostRefunded(
            suggested_post_message_id=suggested_post_message_id,
            reason=reason
        )

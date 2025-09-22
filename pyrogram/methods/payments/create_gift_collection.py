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
import re
from typing import List, Union

import pyrogram
from pyrogram import raw, types


class CreateGiftCollection:
    async def create_gift_collection(
        self: "pyrogram.Client",
        owner_id: Union[int, str],
        name: str,
        gift_ids: List[str]
    ) -> "types.GiftCollection":
        """Creates a collection from gifts on the current user's or a channel's profile page.

        .. include:: /_includes/usable-by/users.rst

        Parameters:
            owner_id (``int`` | ``str``):
                Unique identifier (int) or username (str) of the target chat.
                For your personal cloud (Saved Messages) you can simply use "me" or "self".

            name (``str``):
                Name of the collection, 1-12 characters.

            gift_ids (List of ``str``):
                Identifier of the gifts to add to the collection.

        Returns:
            :obj:`~pyrogram.types.GiftCollection`: On success, a created collection is returned.

        Example:
            .. code-block:: python

                await create_gift_collection("me", "My best gifts!", ["https://t.me/nft/NekoHelmet-9215"])
        """
        stargifts = []

        for gift in gift_ids:
            if not isinstance(gift, str):
                raise ValueError(f"gift id has to be str, but {type(gift)} was provided")

            saved_gift_match = re.match(r"^(-\d+)_(\d+)$", gift)
            slug_match = self.UPGRADED_GIFT_RE.match(gift)

            if saved_gift_match:
                stargifts.append(
                    raw.types.InputSavedStarGiftChat(
                        peer=await self.resolve_peer(saved_gift_match.group(1)),
                        saved_id=int(saved_gift_match.group(2))
                    )
                )
            elif slug_match:
                stargifts.append(
                    raw.types.InputSavedStarGiftSlug(
                        slug=slug_match.group(1)
                    )
                )
            else:
                stargifts.append(
                    raw.types.InputSavedStarGiftUser(
                        msg_id=int(gift)
                    )
                )

        r = await self.invoke(
            raw.functions.payments.CreateStarGiftCollection(
                peer=await self.resolve_peer(owner_id),
                title=name,
                stargift=stargifts
            )
        )

        return await types.GiftCollection._parse(self, r)

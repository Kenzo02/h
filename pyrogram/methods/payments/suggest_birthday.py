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


from typing import Union

import pyrogram
from pyrogram import raw, types


class SuggestBirthday:
    async def suggest_birthday(
        self: "pyrogram.Client",
        chat_id: Union[int, str],
        birthday: "types.Birthday"
    ) -> bool:
        """Drops original details for an upgraded gift.

        .. include:: /_includes/usable-by/users.rst

        Parameters:
            chat_id (``int`` | ``str``):
                Unique identifier (int) or username (str) of the target chat.
                For your personal cloud (Saved Messages) you can simply use "me" or "self".
                For a contact that exists in your Telegram address book you can use his phone number (str).

            birthday (:obj:`types.Birthday`):
                Birthday in the format "YYYY-MM-DD".

        Returns:
            ``bool``: On success, True is returned.

        Example:
            .. code-block:: python

                await app.suggest_birthday(chat_id=123456, birthday=types.Birthday(day=1, month=1, year=2000))
        """
        await self.invoke(
            raw.functions.users.SuggestBirthday(
                id=await self.resolve_peer(chat_id),
                birthday=birthday.write()
            )
        )

        return True

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

import logging
from typing import List

import pyrogram
from pyrogram import raw
from pyrogram import types
from pyrogram.errors import PhoneMigrate, NetworkMigrate

log = logging.getLogger(__name__)


class SendCode:
    async def send_code(
        self: "pyrogram.Client",
        phone_number: str,
        current_number: bool = None,
        allow_flashcall: bool = None,
        allow_app_hash: bool = None,
        allow_missed_call: bool = None,
        allow_firebase: bool = None,
        logout_tokens: List[bytes] = None,
        token: str = None,
        recaptcha_token: str = None,
        app_sandbox: bool = None,
    ) -> "types.SentCode":
        """Send the confirmation code to the given phone number.

        .. include:: /_includes/usable-by/users.rst

        Parameters:
            phone_number (``str``):
                Phone number in international format (includes the country prefix).

            current_number (``bool``, *optional*):
                Whether the phone number is the current one.

            allow_flashcall (``bool``, *optional*):
                Whether to allow a flash call.

            allow_app_hash (``bool``, *optional*):
                Whether to allow an app hash.

            allow_missed_call (``bool``, *optional*):
                Whether to allow a missed call.

            allow_firebase (``bool``, *optional*):
                Whether to allow firebase.

            logout_tokens (List of ``bytes``, *optional*):
                List of logout tokens.

            token (``str``, *optional*):
                Token.

            recaptcha_token (``str``, *optional*):
                Recaptcha token.

            app_sandbox (``bool``, *optional*):
                Whether to use the app sandbox.

        Returns:
            :obj:`~pyrogram.types.SentCode`: On success, an object containing information on the sent confirmation code
            is returned.

        Raises:
            BadRequest: In case the phone number is invalid.
        """
        phone_number = phone_number.strip(" +")

        while True:
            try:
                r = await self.invoke(
                    raw.functions.auth.SendCode(
                        phone_number=phone_number,
                        api_id=self.api_id,
                        api_hash=self.api_hash,
                        settings=raw.types.CodeSettings(
                            allow_flashcall=allow_flashcall,
                            current_number=current_number,
                            allow_app_hash=allow_app_hash,
                            allow_missed_call=allow_missed_call,
                            allow_firebase=allow_firebase,
                            logout_tokens=logout_tokens,
                            token=token,
                            app_sandbox=app_sandbox
                        )
                    ),
                    recaptcha_token=recaptcha_token
                )
            except (PhoneMigrate, NetworkMigrate) as e:
                dc_option = await self.get_dc_option(e.value, ipv6=self.ipv6)
                await self.session.stop()

                self.session = await self.get_session(
                    dc_id=e.value,
                    server_address=dc_option.ip_address,
                    port=dc_option.port,
                    export_authorization=False,
                    temporary=True
                )

                await self.storage.dc_id(e.value)
                await self.storage.server_address(dc_option.ip_address)
                await self.storage.port(dc_option.port)
                await self.storage.auth_key(self.session.auth_key)
            else:
                return types.SentCode._parse(r)

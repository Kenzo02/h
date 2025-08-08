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

import asyncio
import base64
import datetime
import logging
from typing import List, Optional

import pyrogram
from pyrogram import filters, handlers, raw, types
from pyrogram.session import Session
from pyrogram.session.auth import Auth

log = logging.getLogger(__name__)


async def get_session(client: "pyrogram.Client", dc_id: int) -> Session:
    if dc_id == await client.storage.dc_id():
        return client.session

    async with client.media_sessions_lock:
        if client.media_sessions.get(dc_id):
            return client.media_sessions[dc_id]

        session = client.media_sessions[dc_id] = Session(
            client,
            dc_id,
            await Auth(client, dc_id, await client.storage.test_mode()).create(),
            await client.storage.test_mode(),
            is_media=False,
        )

        await session.start()

        return session


class QRLogin:
    def __init__(self, client, except_ids: List[int] = []):
        self.client: "pyrogram.Client" = client
        self.except_ids: List[int] = except_ids
        self.r: "raw.base.auth.LoginToken" = None

    async def recreate(self):
        self.r = await self.client.invoke(
            raw.functions.auth.ExportLoginToken(
                api_id=self.client.api_id,
                api_hash=self.client.api_hash,
                except_ids=self.except_ids,
            )
        )

        if isinstance(self.r, raw.types.auth.LoginTokenMigrateTo):
            await self.client.storage.dc_id(self.r.dc_id)
            self.client.session = await get_session(self.client, self.r.dc_id)
            self.r = await self.client.invoke(
                raw.functions.auth.ImportLoginToken(token=self.r.token)
            )

        return self.r

    async def wait(self, timeout: float = None) -> Optional["types.User"]:
        if timeout is None:
            if not self.r:
                raise asyncio.TimeoutError

            timeout = self.r.expires - int(datetime.datetime.now().timestamp())

        event = asyncio.Event()

        async def raw_handler(client, update, users, chats):
            event.set()

        handler = self.client.add_handler(
            handlers.RawUpdateHandler(
                raw_handler,
                filters=filters.create(lambda _, __, u: isinstance(u, raw.types.UpdateLoginToken)),
            )
        )

        await self.client.dispatcher.start()

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        finally:
            self.client.remove_handler(*handler)
            await self.client.dispatcher.stop(clear=False)

        await self.recreate()

        if isinstance(self.r, raw.types.auth.LoginTokenSuccess):
            user = types.User._parse(self.client, self.r.authorization.user)

            await self.client.storage.user_id(user.id)
            await self.client.storage.is_bot(False)

            return user

        raise TypeError("Unexpected login token response: {}".format(self.r))

    @property
    def url(self) -> str:
        return f"tg://login?token={base64.urlsafe_b64encode(self.r.token).decode('utf-8')}"

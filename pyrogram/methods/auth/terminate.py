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

from __future__ import annotations as _annotations

import logging

import pyrogram
from pyrogram import raw
from pyrogram.session.session import SessionCleanupError

log = logging.getLogger(__name__)


class Terminate:
    async def terminate(self: pyrogram.Client, clear_handlers: bool = True):
        """Terminate the client by shutting down workers.

        This method does the opposite of :meth:`~pyrogram.Client.initialize`.
        It will stop the dispatcher and shut down updates and download workers.

        Parameters:
            clear_handlers (``bool``, *optional*):
                Clear the already existing handlers on restart the client.
                Default to True.

        Raises:
            ConnectionError: In case you try to terminate a client that is already terminated.
        """
        if not self.is_initialized:
            raise ConnectionError("Client is already terminated")

        if self.takeout_id:
            await self.invoke(raw.functions.account.FinishTakeoutSession())
            log.info("Takeout session %s finished", self.takeout_id)

        await self.storage.save()
        await self.dispatcher.stop(clear_handlers=clear_handlers)

        failures = []
        # Preserve the established media -> auxiliary -> watchdog ordering, but
        # a rejected close must not strand independent sessions. Keep failed
        # resources (and any originating worker) reachable for diagnostics.
        for kind, sessions in (("media", self.media_sessions), ("auxiliary", self.sessions)):
            for dc_id, session in list(sessions.items()):
                try:
                    await session.stop()
                except SessionCleanupError as error:
                    failures.extend(
                        (f"{kind}-{dc_id}-{phase}", cause) for phase, cause in error.failures
                    )
                else:
                    if not session._has_live_workers():
                        sessions.pop(dc_id, None)

        self.updates_watchdog_event.set()

        if self.updates_watchdog_task is not None:
            await self.updates_watchdog_task

        self.updates_watchdog_event.clear()

        self.is_initialized = False
        if failures:
            raise SessionCleanupError(failures)

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

from typing import TYPE_CHECKING

from pyrogram.session.session import SessionCleanupError, SessionState

if TYPE_CHECKING:
    import pyrogram


class Disconnect:
    async def disconnect(
        self: pyrogram.Client,
    ):
        """Disconnect the client from Telegram servers.

        Raises:
            ConnectionError: In case you try to disconnect an already disconnected client or in case you try to
                disconnect a client that needs to be terminated first.
        """
        if not self.is_connected:
            raise ConnectionError("Client is already disconnected")

        if self.is_initialized:
            raise ConnectionError("Can't disconnect an initialized client")

        cleanup_error = None
        try:
            await self.session.stop()
        except SessionCleanupError as error:
            cleanup_error = error

        sessions = [self.session, *self.media_sessions.values(), *self.sessions.values()]
        # A stopped transport does not imply a worker requesting its own stop
        # has returned. Storage must stay open while any such worker can use it.
        if any(session._has_live_workers() for session in sessions):
            failures = list(cleanup_error.failures) if cleanup_error is not None else []
            failures.append(("workers", RuntimeError("Client workers are still running")))
            # Retain the client/storage owner so disconnect can be retried after
            # the originating worker has returned.
            raise SessionCleanupError(failures)
        await self.storage.close()
        if cleanup_error is not None:
            raise cleanup_error
        # A self-stopping worker kept its cache entry alive during terminate.
        # On a safe retry, retire that stopped generation; failed closes remain
        # owned for diagnostics instead of being silently discarded.
        for cache in (self.media_sessions, self.sessions):
            for dc_id, session in list(cache.items()):
                if session.state is SessionState.STOPPED:
                    cache.pop(dc_id, None)
        self.session = None
        self.is_connected = False

from __future__ import annotations as _annotations

import pyrogram
from pyrogram import raw, types


class GetStickers:
    async def get_stickers(self: pyrogram.Client, short_name: str) -> list[types.Sticker]:
        """Get all stickers from a set, preserving the legacy list-returning API.

        .. include:: /_includes/usable-by/users-bots.rst

        Parameters:
            short_name (``str``):
                Short name of the sticker set.

        Returns:
            List of :obj:`~pyrogram.types.Sticker`: Stickers in the set.
        """
        # Do not alias get_sticker_set: it returns metadata rather than the old list,
        # and may fetch thumbnails unrelated to the caller's requested stickers.
        sticker_set = await self.invoke(
            raw.functions.messages.GetStickerSet(
                stickerset=raw.types.InputStickerSetShortName(short_name=short_name), hash=0
            )
        )
        return types.List(
            [
                await types.Sticker._parse(self, doc, {type(a): a for a in doc.attributes})
                for doc in sticker_set.documents
            ]
        )

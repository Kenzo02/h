"""Opt-in admission of ordinary channel messages to parsed consumers.

False means raw-only, not drop: metadata already carried by Telegram, raw
handlers, FIFO update admission and pts persistence remain intact. Consumers
must decide from their complete native-ingress interests, not only one handler.
The decision belongs to admission; a later subscription cannot replay an older
raw-only packet into parsed handlers. Defaults and uncertain decisions hydrate
and parse normally. Recovery and service updates do not use this optimization.
"""

from __future__ import annotations as _annotations

import inspect
import logging
from typing import TYPE_CHECKING

from pyrogram import raw

if TYPE_CHECKING:
    from pyrogram import Client

log = logging.getLogger(__name__)


def should_parse_update(client: Client, update: raw.base.Update) -> bool:
    policy = getattr(client, "update_parser_policy", None)
    if policy is None or not (
        isinstance(update, raw.types.UpdateNewChannelMessage)
        and isinstance(update.message, raw.types.Message)
    ):
        return True
    try:
        decision = policy(update)
    except Exception as error:
        log.warning(
            "update_parser_policy_failed owner=update_ingress error_type=%s "
            "action=hydrate_and_parse",
            type(error).__name__,
        )
        return True
    if type(decision) is not bool:
        # Reject accidentally async policies without leaving an unawaited coroutine.
        if inspect.iscoroutine(decision):
            decision.close()
        log.warning(
            "update_parser_policy_invalid owner=update_ingress result_type=%s "
            "action=hydrate_and_parse",
            type(decision).__name__,
        )
        return True
    return decision

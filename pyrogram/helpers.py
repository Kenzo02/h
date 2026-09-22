from __future__ import annotations as _annotations

import asyncio
import logging

log = logging.getLogger(__name__)


def log_task_exception(task: asyncio.Task) -> None:
    """Retrieve and log the exception from a finished Task."""
    if task.cancelled():
        return

    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    except Exception as e:
        log.exception("Error while retrieving task exception: %s", e)
        return

    if exc:
        log.error("Unhandled exception in background task", exc_info=exc)

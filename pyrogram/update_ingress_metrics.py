"""Fixed-size, content-free native-ingress counters with a bounded observer.

Counters describe SDK admission/custody, never successful customer publication.
No per-peer/event retention, timer task or I/O. Cumulative integers saturate;
reports occur on activity at most once per minute. Observers must be synchronous.
"""

from __future__ import annotations as _annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrogram import Client

log = logging.getLogger(__name__)
KEYS = (
    "full",
    "raw_only",
    "hydration_attempts",
    "hydration_completed",
    "hydration_fallbacks",
    "tasks_accepted",
    "tasks_completed",
    "tasks_failed",
    "tasks_cancelled",
    "reconnects",
    "reconnect_ready",
    "reconnect_failed",
    "retained_on_reconnect",
    "terminal_cancelled",
    "observer_errors",
)
MAX_COUNT = (1 << 63) - 1


class UpdateIngressMetrics:
    __slots__ = ("counts", "last_report")

    def __init__(self) -> None:
        self.counts: dict[str, int] = dict.fromkeys(KEYS, 0)
        self.last_report = time.monotonic()

    def record(self, client: Client, key: str, amount: int = 1) -> None:
        self.counts[key] = min(MAX_COUNT, self.counts[key] + amount)
        observer = getattr(client, "update_ingress_observer", None)
        now = time.monotonic()
        if not callable(observer) or now - self.last_report < 60:
            return
        elapsed_ms = int((now - self.last_report) * 1000)
        self.last_report = now
        try:
            result = observer({**self.counts, "interval_ms": elapsed_ms})
            if inspect.iscoroutine(result):
                result.close()
                raise TypeError("synchronous observer required")
        except (Exception, asyncio.CancelledError):
            self.counts["observer_errors"] = min(MAX_COUNT, self.counts["observer_errors"] + 1)
            # Observer errors cannot break native acceptance; never log its payload.
            log.error("update_ingress_observer_failed owner=sdk_native action=retain_processing")


def record_ingress(client: Client, key: str, amount: int = 1) -> None:
    metrics = getattr(client, "_update_ingress_metrics", None)
    if isinstance(metrics, UpdateIngressMetrics):
        metrics.record(client, key, amount)

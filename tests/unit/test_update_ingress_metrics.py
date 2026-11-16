"""Public ingress evidence; absent metrics on baseline fails behavior, not import."""

from __future__ import annotations as _annotations

import asyncio

import pytest

from tests.unit.test_update_parser_policy import client as client, packet  # noqa: PLC0414 (pytest fixture)


async def test_observer_counts_raw_full_and_hydration_without_per_update_logs(client):
    reports = []
    client.update_ingress_observer = reports.append
    client.update_parser_policy = lambda update: update.message.peer_id.channel_id == 202
    await client.handle_updates(packet(1))
    await client.handle_updates(packet(2, 202))
    metrics = getattr(client, "_update_ingress_metrics", None)
    if metrics is not None:
        metrics.last_report -= 61
    await client.handle_updates(packet(3))
    assert len(reports) == 1
    report = reports[0]
    assert report["raw_only"] == 2 and report["full"] == 1
    assert report["hydration_attempts"] == report["hydration_completed"] == 1
    assert all(type(value) is int for value in report.values())
    for i in range(4, 204):
        await client.handle_updates(packet(i))
    assert len(reports) == 1
    assert set(report) == set(metrics.counts) | {"interval_ms"}
    metrics.counts["raw_only"] = (1 << 63) - 1
    await client.handle_updates(packet(204))
    assert metrics.counts["raw_only"] == (1 << 63) - 1


@pytest.mark.parametrize("async_observer", [False, True, "cancelled"])
async def test_broken_observer_is_bounded_sanitized_and_cannot_block_dispatch(
    client, caplog, async_observer
):
    def broken(_):
        if async_observer == "cancelled":
            raise asyncio.CancelledError("private-content-not-for-logs")
        raise ValueError("private-content-not-for-logs")

    async def broken_async(_):
        pass

    client.update_ingress_observer = broken_async if async_observer is True else broken
    metrics = getattr(client, "_update_ingress_metrics", None)
    if metrics is not None:
        metrics.last_report -= 61
    await client.handle_updates(packet(1, 202))
    await client.handle_updates(packet(2, 202))
    assert client.dispatcher.updates_queue.qsize() == 2
    assert caplog.text.count("update_ingress_observer_failed") == 1
    assert "private-content-not-for-logs" not in caplog.text
    assert metrics.counts["observer_errors"] == 1

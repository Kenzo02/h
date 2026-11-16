"""Accepted native updates survive transport reconnect, not terminal stop.

Real encrypted packet ingress, Session lifecycle, Client lock and SQLite cursors;
only the provider wire is controlled. No new implementation API in red setup.
"""

from __future__ import annotations as _annotations

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from pyrogram import raw
from pyrogram.crypto import aes, mtproto
from pyrogram.raw.core import Message
from pyrogram.storage import UpdateState
from tests.unit.session.test_auxiliary_startup import AUTH_KEY, wait_until
from tests.unit.session.test_startup_receiver_lifecycle import native as native  # noqa: PLC0414 (pytest fixture)
from tests.unit.test_update_parser_policy import channel, packet, user

CHANNEL = -1000000000101


def incoming(pts, kind="new"):
    value = packet(pts - 100)
    update = value.updates[0]
    update.message.id = pts
    update.message.from_id = raw.types.PeerUser(user_id=42)
    value.users = [user(42)]
    if kind == "edit":
        update = raw.types.UpdateEditChannelMessage(message=update.message, pts=pts, pts_count=1)
    elif kind == "delete":
        update = raw.types.UpdateDeleteChannelMessages(
            channel_id=101, messages=[pts], pts=pts, pts_count=1
        )
    elif kind == "common_edit":
        update.message.peer_id = raw.types.PeerUser(user_id=42)
        update = raw.types.UpdateEditMessage(message=update.message, pts=pts, pts_count=1)
    elif kind == "common_delete":
        update = raw.types.UpdateDeleteMessages(messages=[pts], pts=pts, pts_count=1)
    value.updates = [update]
    return value


async def ready(h):
    c = h.client
    c.skip_updates = True
    await c.connect()
    h.owned.append(c.session)
    c.dispatcher._accepting_updates = True
    c.session.STOP_TIMEOUT = 0.01
    return c, c.session


async def receive(session, value):
    await session.connection.reply(value, session.session_id)


async def pending_update(c):
    await wait_until(lambda: bool(c._update_state_lock._waiters))


def delivered(c):
    values = []
    while not c.dispatcher.updates_queue.empty():
        update, _, _ = c.dispatcher.updates_queue.get_nowait()
        values.append(update.pts)
    return values


@pytest.mark.parametrize("initial", [None, 10])
@pytest.mark.parametrize("kind", ["new", "edit", "delete", "common_edit", "common_delete"])
@pytest.mark.parametrize("reconnects", [1, 2])
async def test_reconnect_does_not_supersede_accepted_pts11_with_live_pts12(
    native, initial, kind, reconnects
):
    c, session = await ready(native)
    state_id = 0 if kind.startswith("common") else CHANNEL
    if initial is not None:
        await c.storage.set_update_state(UpdateState(state_id, initial, None, 1, 1))
    await c._update_state_lock.acquire()
    try:
        await receive(session, incoming(11, kind))
        await pending_update(c)
        # Native reception/ACK bookkeeping has already accepted pts11.
        assert session.pending_acks
        for _ in range(reconnects):
            await asyncio.wait_for(session.restart(), 3)
        await receive(session, incoming(12, kind))
        await asyncio.sleep(0.02)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    seen = delivered(c)
    assert (await c.storage.get_update_states(state_id))[0].pts == 12
    # The reviewer's subsequent recover_gaps() cannot recover 11 from cursor12.
    answer = (
        raw.types.updates.DifferenceEmpty(date=2, seq=2)
        if state_id == 0
        else raw.types.updates.ChannelDifferenceEmpty(pts=12, final=True)
    )
    c.invoke = AsyncMock(return_value=answer)
    assert await c.recover_gaps(state_id) == (0, 0)
    assert c.invoke.call_args.args[0].pts == 12
    assert seen == [11, 12]  # RED before fix: [12], including first-seen channels.
    metrics = getattr(c, "_update_ingress_metrics", None)
    if metrics is not None:
        assert metrics.counts["retained_on_reconnect"] >= 1
        assert metrics.counts["tasks_cancelled"] == 0
        assert metrics.counts["tasks_completed"] >= 2


@pytest.mark.parametrize("initial", [None, 10])
async def test_hydration_waiter_retries_on_new_transport_without_losing_accepted_update(
    native, initial
):
    c, session = await ready(native)
    if initial is not None:
        await c.storage.set_update_state(UpdateState(CHANNEL, initial, None, 1, 1))
    entered = asyncio.Event()
    calls = []
    factory = c.connection_factory

    def instrument(wire, lose_response=False):
        send = wire.send

        async def provider(payload):
            key, iv = mtproto.kdf(AUTH_KEY, payload[8:24], True)
            plain = BytesIO(aes.ige256_decrypt(payload[24:], key, iv))
            plain.read(8)
            sid = plain.read(8)
            request = Message.read(plain)
            query = request.body
            if not isinstance(query, raw.functions.updates.GetChannelDifference):
                return await send(payload)
            calls.append(query.filter.ranges[0].min_id)
            if lose_response:
                entered.set()
                return  # Actual Session.send waits; reconnect fails that RPC result.
            await wire.reply(
                raw.types.RpcResult(
                    req_msg_id=request.msg_id,
                    result=raw.types.updates.ChannelDifference(
                        pts=11,
                        final=True,
                        new_messages=[],
                        other_updates=[],
                        users=[user(42)],
                        chats=[channel(101)],
                    ),
                ),
                sid,
            )

        wire.send = provider
        return wire

    instrument(session.connection, True)
    c.connection_factory = lambda **kw: instrument(factory(**kw))
    first = incoming(11)
    first.users = [user(42, True)]
    await receive(session, first)
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(session.restart(), 3)
    await receive(session, incoming(12))
    await asyncio.wait_for(wait_until(lambda: not session.pending_tasks), 5)
    assert delivered(c) == [11, 12]
    assert calls == [11, 11]
    assert (await c.storage.get_update_states(CHANNEL))[0].pts == 12


async def test_terminal_stop_still_discards_pending_native_work_without_history_replay(native):
    c, session = await ready(native)
    await c._update_state_lock.acquire()
    try:
        await receive(session, incoming(11))
        await pending_update(c)
        await asyncio.wait_for(session.stop(), 3)
        await session.restart()
        assert not session.pending_tasks
        assert session._must_stay_stopped
        assert len(native.transports) == 1
        assert delivered(c) == []
        assert await c.storage.get_update_states(CHANNEL) == []
        metrics = getattr(c, "_update_ingress_metrics", None)
        if metrics is not None:
            assert metrics.counts["terminal_cancelled"] == 1
            assert metrics.counts["tasks_cancelled"] == 1
            assert metrics.counts["tasks_completed"] == 0
    finally:
        c._update_state_lock.release()


async def test_terminal_stop_between_reconnect_close_and_start_releases_retained_work(native):
    c, session = await ready(native)
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.disconnect_handler

    async def disconnected(app, current):
        if callable(original):
            await original(app, current)
        entered.set()
        await release.wait()

    c.disconnect_handler = disconnected
    await c._update_state_lock.acquire()
    owner = None
    try:
        await receive(session, incoming(11))
        await pending_update(c)
        owner = asyncio.create_task(session.restart())
        await asyncio.wait_for(entered.wait(), 3)
        # STOPPED transport still owns an accepted task: terminal stop must drain it.
        await asyncio.wait_for(session.stop(), 3)
        assert not session.pending_tasks
        release.set()
        await asyncio.wait_for(owner, 3)
        assert len(native.transports) == 1
        assert delivered(c) == []
    finally:
        release.set()
        c._update_state_lock.release()
        if owner is not None:
            await asyncio.gather(owner, return_exceptions=True)


async def test_failed_reconnect_attempt_keeps_custody_through_backoff(native):
    c, session = await ready(native)
    native.options = [{"mode": "error"}]
    session.fallback_endpoints = session.fallback_endpoints[:1]
    await c._update_state_lock.acquire()
    try:
        await receive(session, incoming(11))
        await pending_update(c)
        session.schedule_restart("recv-null")
        await asyncio.wait_for(session.restart_task, 3)
        assert len(native.transports) == 3
        await receive(session, incoming(12))
        await asyncio.sleep(0.02)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == [11, 12]

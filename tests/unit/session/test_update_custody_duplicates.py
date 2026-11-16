"""Retransmissions use real encrypted ingress; custody must not replay work."""

from __future__ import annotations as _annotations

import asyncio
import hashlib
import os

import pytest

from pyrogram import raw
from pyrogram.crypto import aes, mtproto
from pyrogram.raw.core import Long, Message
from tests.unit.session.test_auxiliary_startup import AUTH_KEY, wait_until
from tests.unit.session.test_startup_receiver_lifecycle import native as native  # noqa: PLC0414 (pytest fixture)
from tests.unit.session.test_update_reconnect_custody import delivered, incoming, ready, receive


async def captured_receive(session, value):
    wire = session.connection
    # A valid provider ID within the allowed future skew, above later fixture
    # handshake IDs. Otherwise the unrelated lower-than-all security check can
    # mask the broken duplicate fence and make an identical-ciphertext test pass.
    msg_id = await session.msg_factory.allocate_message_identity() + 1 + (5 << 32)
    wire.seq += 1
    message = Message(value, msg_id, 2 * wire.seq + 1, len(value))
    data = Long(0) + session.session_id + message.write()
    padding = os.urandom(-(len(data) + 12) % 16 + 12)
    msg_key = hashlib.sha256(AUTH_KEY[96:128] + data + padding).digest()[8:24]
    key, iv = mtproto.kdf(AUTH_KEY, msg_key, False)
    ciphertext = (
        hashlib.sha1(AUTH_KEY).digest()[-8:] + msg_key + aes.ige256_encrypt(data + padding, key, iv)
    )
    await wire.incoming.put(ciphertext)
    await wait_until(lambda: msg_id in session.stored_msg_ids)
    return ciphertext


@pytest.mark.parametrize("kind", ["new", "edit", "delete", "common_edit", "common_delete"])
@pytest.mark.parametrize("envelope", ["identical_twice", "fresh"])
async def test_retained_update_not_replayed_by_envelope(native, kind, envelope):
    c, session = await ready(native)
    await c._update_state_lock.acquire()
    try:
        original = await captured_receive(session, incoming(11, kind))
        await session.restart()
        if envelope == "identical_twice":
            await session.restart()
            await session.connection.incoming.put(original)
        else:
            duplicate = incoming(11, kind)
            duplicate.users.append(raw.types.User(id=7, access_hash=7, first_name="fixture"))
            await receive(session, duplicate)
        await receive(session, incoming(12, kind))
        await asyncio.sleep(0.03)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == [11, 12]
    if envelope == "fresh":
        assert (await c.storage.get_peer_by_id(7)).user_id == 7


async def test_retained_wire_identity_outlives_thirty_id_window(native):
    c, session = await ready(native)
    custody = getattr(session, "_update_custody", None)
    if custody is not None:
        custody.COMPLETED_LIMIT = 2
    await c._update_state_lock.acquire()
    try:
        for pts in range(11, 51):
            ciphertext = await captured_receive(session, incoming(pts))
        await session.restart()
        await session.connection.incoming.put(ciphertext)
        await receive(session, incoming(51))
        await asyncio.sleep(0.03)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == list(range(11, 52))
    if custody is not None:
        assert not custody.messages and not custody.ranges
        assert len(custody.completed) <= 2


async def test_completed_retained_range_rejects_late_fresh_envelope(native):
    c, session = await ready(native)
    await c._update_state_lock.acquire()
    try:
        await captured_receive(session, incoming(11))
        await session.restart()
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    await receive(session, incoming(12))
    await wait_until(lambda: c.dispatcher.updates_queue.qsize() == 2)
    await wait_until(lambda: not session.pending_tasks)
    await receive(session, incoming(11))
    await asyncio.sleep(0.03)
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == [11, 12]


@pytest.mark.parametrize("short", [False, True])
async def test_same_message_distinct_edit_revisions_survive_pending_dedupe(native, short):
    c, session = await ready(native)

    def edit(pts):
        value = incoming(pts, "common_edit")
        value.updates[0].message.id = 777
        return raw.types.UpdateShort(update=value.updates[0], date=1) if short else value

    await c._update_state_lock.acquire()
    try:
        await captured_receive(session, edit(11))
        await session.restart()
        await receive(session, edit(11))
        await receive(session, edit(12))
        await receive(session, edit(13))
        await asyncio.sleep(0.03)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == [11, 12, 13]


async def test_pending_pts_is_scoped_and_mixed_batch_keeps_novel_updates(native):
    c, session = await ready(native)
    await c._update_state_lock.acquire()
    try:
        await captured_receive(session, incoming(11))
        await session.restart()
        value = incoming(11)
        other_channel = incoming(11).updates[0]
        other_channel.message.peer_id.channel_id = 202
        value.updates.extend([incoming(12).updates[0], other_channel])
        await receive(session, value)
        await asyncio.sleep(0.03)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == [11, 12, 11]


async def test_unseen_lower_pts_is_not_inferred_duplicate_from_max_cursor(native):
    c, session = await ready(native)
    # The existing SDK's stored cursor is a last-written position, not a proven
    # contiguous receipt ledger. Do not turn its gap into silent data loss.
    await receive(session, incoming(13, "edit"))
    await wait_until(lambda: c.dispatcher.updates_queue.qsize() == 1)
    await receive(session, incoming(12, "edit"))
    await wait_until(lambda: c.dispatcher.updates_queue.qsize() == 2)
    await wait_until(lambda: not session.pending_tasks)
    assert delivered(c) == [13, 12]


async def test_fault_diagnostic_without_observer_is_bounded_and_sanitized(native, caplog):
    c, session = await ready(native)
    assert c.update_ingress_observer is None

    async def broken(_):
        raise ValueError("PRIVATE_PAYLOAD_MUST_NOT_APPEAR")

    c.handle_updates = broken
    caplog.set_level("ERROR", logger="pyrogram.session.session")
    for pts in (11, 12, 13):
        await receive(session, incoming(pts))
        await asyncio.sleep(0.03)
    await wait_until(lambda: not session.pending_tasks)
    records = [r for r in caplog.records if "accepted_update_failed" in r.getMessage()]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "operation=client.handle_updates" in text and "error_type=ValueError" in text
    assert "PRIVATE_PAYLOAD_MUST_NOT_APPEAR" not in caplog.text
    assert c._update_ingress_metrics.counts["tasks_failed"] == 3
    assert not session._update_custody.messages and not session._update_custody.ranges
    assert not session._update_custody.completed
    session._update_fault_logged_at -= 61
    await receive(session, incoming(14))
    await asyncio.sleep(0.03)
    await wait_until(lambda: not session.pending_tasks)
    records = [r for r in caplog.records if "accepted_update_failed" in r.getMessage()]
    assert len(records) == 2 and "suppressed=2" in records[-1].getMessage()
    assert all(r.exc_info is None for r in records)
    assert "PRIVATE_PAYLOAD_MUST_NOT_APPEAR" not in caplog.text


@pytest.mark.parametrize(("count", "restarts"), [(1, 2), (40, 1)])
async def test_pending_wire_custody_also_protects_updates_without_pts(native, count, restarts):
    c, session = await ready(native)

    def status(user_id):
        return raw.types.Updates(
            updates=[
                raw.types.UpdateUserStatus(
                    user_id=user_id, status=raw.types.UserStatusOffline(was_online=1)
                )
            ],
            users=[],
            chats=[],
            date=1,
            seq=1,
        )

    await c._update_state_lock.acquire()
    ciphertext = None
    try:
        for user_id in range(100, 100 + count):
            ciphertext = await captured_receive(session, status(user_id))
        for _ in range(restarts):
            await session.restart()
        assert ciphertext is not None
        await session.connection.incoming.put(ciphertext)
        await receive(session, status(999))
        await asyncio.sleep(0.03)
    finally:
        c._update_state_lock.release()
    await wait_until(lambda: not session.pending_tasks)
    got = []
    while not c.dispatcher.updates_queue.empty():
        got.append(c.dispatcher.updates_queue.get_nowait()[0].user_id)
    assert got == [*range(100, 100 + count), 999]

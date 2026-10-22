from __future__ import annotations as _annotations

import asyncio
from concurrent.futures import Executor, Future
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pyrogram import raw
from pyrogram.crypto import mtproto
from pyrogram.errors import FloodWait
from pyrogram.session import session as session_module
from pyrogram.session.session import PublicationUnconfirmed, Result, ordinary_publication
from tests.unit.session.test_album_publication_policy import ordinary_query, ordinary_transport
from tests.unit.session.test_session import _server_packet, _started_session


class DeferredPack(Executor):
    """An already-running executor job whose completion the test controls."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.future = Future()
        self.future.set_running_or_notify_cancel()

    def submit(self, fn, /, *args, **kwargs):
        self.job = (fn, args, kwargs)
        self.entered.set()
        return self.future

    def finish(self):
        fn, args, kwargs = self.job
        self.future.set_result(fn(*args, **kwargs))


@pytest.fixture
def rpc(monkeypatch):
    session = _started_session()
    results = []
    waiting = asyncio.Queue()
    resume_wait = asyncio.Event()
    resume_wait.set()

    def observed_result():
        result = Result()
        wait = result.event.wait

        async def observed_wait():
            waiting.put_nowait(result)
            value = await wait()
            await resume_wait.wait()
            return value

        monkeypatch.setattr(result.event, "wait", observed_wait)
        results.append(result)
        return result

    # Observe the real Event.wait, optionally holding signal delivery for stop
    # races. Send, Result, packet handling and stop keep their native implementations.
    # No network or authenticated client.
    monkeypatch.setattr(session_module, "Result", observed_result)
    return SimpleNamespace(
        session=session, results=results, waiting=waiting, resume_wait=resume_wait
    )


async def waiting_result(rpc):
    return await asyncio.wait_for(rpc.waiting.get(), 1)


async def answer(session, msg_id, result):
    await session.handle_packet(
        await _server_packet(
            session=session, body=raw.types.RpcResult(req_msg_id=msg_id, result=result)
        )
    )


async def cancel(task):
    task.cancel("cancel this RPC")
    # Python 3.10 Task.result() can discard the cancellation message. The
    # direct exception and ordinary-publication tests check it inside the caller.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.parametrize("outcome", ["success", "timeout", "rpc_error"])
async def test_terminal_response_removes_only_its_result(rpc, monkeypatch, outcome):
    session = rpc.session
    sibling = session.results[-1] = Result()
    response = raw.types.Pong(msg_id=1, ping_id=1)
    if outcome == "rpc_error":
        response = raw.types.RpcError(error_code=420, error_message="FLOOD_WAIT_2")

    async def send(payload):
        session.connection.sent.append(payload)
        if outcome != "timeout":
            await answer(session, next(key for key in session.results if key != -1), response)

    monkeypatch.setattr(session.connection, "send", send)
    if outcome == "success":
        result = await session.send(raw.functions.Ping(ping_id=1), timeout=0.01)
        assert isinstance(result, raw.types.Pong) and result.ping_id == 1
    else:
        with pytest.raises(FloodWait if outcome == "rpc_error" else TimeoutError):
            await session.send(raw.functions.Ping(ping_id=1), timeout=0.01)
    assert not rpc.waiting.empty()
    assert session.results == {-1: sibling}
    assert len(session.connection.sent) == 1


@pytest.mark.parametrize("stage", ["create", "pack", "transport", "response"])
async def test_cancellation_at_each_await_cleans_pending_result(rpc, monkeypatch, stage):
    session = rpc.session
    entered = asyncio.Event()
    executor = None
    if stage == "pack":
        executor = DeferredPack()
        session.connection.protocol.crypto_executor = executor
        entered = executor.entered
    elif stage in ("create", "transport"):

        async def block(*args):
            entered.set()
            await asyncio.Event().wait()

        target, name = (
            (session.msg_factory, "create") if stage == "create" else (session.connection, "send")
        )
        monkeypatch.setattr(target, name, block)

    task = asyncio.create_task(session.send(raw.functions.Ping(ping_id=1)))
    if stage == "response":
        await waiting_result(rpc)
    else:
        await asyncio.wait_for(entered.wait(), 1)
    await cancel(task)
    if stage == "pack":
        # Cancelling the await cannot stop a running crypto job. Finishing it
        # later must neither resurrect the Result nor send its packed payload.
        assert executor is not None
        assert not executor.future.cancelled()
        executor.finish()
        await asyncio.sleep(0)
    assert session.results == {}
    assert len(rpc.results) == (0 if stage == "create" else 1)
    assert len(session.connection.sent) == (1 if stage == "response" else 0)


@pytest.mark.parametrize("stage", ["pack", "submit", "transport"])
@pytest.mark.parametrize("error_type", [ValueError, OSError, asyncio.CancelledError])
async def test_pre_response_failure_preserves_exception_and_cleans_result(
    rpc, monkeypatch, stage, error_type
):
    session = rpc.session
    error = error_type("fixture failure")

    def fail(*args, **kwargs):
        raise error

    if stage == "pack":
        monkeypatch.setattr(mtproto, "pack", fail)
    elif stage == "submit":
        monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", fail)
    else:
        monkeypatch.setattr(session.connection, "send", AsyncMock(side_effect=error))
    with pytest.raises(error_type) as caught:
        await session.send(raw.functions.Ping(ping_id=1))
    assert caught.value is error
    assert session.results == {}


@pytest.mark.parametrize("outcome", ["success", "timeout", "cancel", "transport_error"])
async def test_cleanup_does_not_remove_replacement_result(rpc, monkeypatch, outcome):
    session = rpc.session
    replacement = Result()
    replaced_id = None

    async def send(payload):
        nonlocal replaced_id
        replaced_id, original = next(iter(session.results.items()))
        session.results[replaced_id] = replacement
        if outcome == "transport_error":
            raise OSError("fixture transport failure")
        if outcome == "success":
            original.value = raw.types.Pong(msg_id=replaced_id, ping_id=1)
            original.event.set()

    monkeypatch.setattr(session.connection, "send", send)
    if outcome == "cancel":
        task = asyncio.create_task(session.send(raw.functions.Ping(ping_id=1)))
        await waiting_result(rpc)
        await cancel(task)
    elif outcome in ("timeout", "transport_error"):
        with pytest.raises(OSError):
            await session.send(raw.functions.Ping(ping_id=1), timeout=0)
    else:
        assert isinstance(await session.send(raw.functions.Ping(ping_id=1)), raw.types.Pong)
    assert session.results == {replaced_id: replacement}
    assert replacement.value is None and not replacement.event.is_set()


@pytest.mark.parametrize("failure", [False, True])
async def test_no_response_send_does_not_own_an_existing_result(rpc, monkeypatch, failure):
    session = rpc.session
    message = await session.msg_factory.create(raw.functions.Ping(ping_id=1))
    monkeypatch.setattr(session.msg_factory, "create", AsyncMock(return_value=message))
    sibling = session.results[message.msg_id] = Result()
    if failure:
        monkeypatch.setattr(session.connection, "send", AsyncMock(side_effect=OSError("fixture")))
        with pytest.raises(OSError):
            await session.send(message.body, wait_response=False)
    else:
        assert await session.send(message.body, wait_response=False) is None
    assert rpc.results == []
    assert session.results == {message.msg_id: sibling}


async def test_late_large_response_cannot_retain_cancelled_rpc_or_disrupt_sibling(rpc):
    session = rpc.session
    first = asyncio.create_task(session.send(raw.functions.Ping(ping_id=1)))
    abandoned = await waiting_result(rpc)
    abandoned_id = next(iter(session.results))
    second = asyncio.create_task(session.send(raw.functions.Ping(ping_id=2)))
    sibling = await waiting_result(rpc)
    sibling_id = next(key for key in session.results if key != abandoned_id)
    await cancel(first)
    late = raw.types.upload.File(
        type=raw.types.storage.FileUnknown(), mtime=0, bytes=b"x" * (1024 * 1024)
    )
    try:
        # Real encrypted packet decoding and response dispatch, not a dict-only fake.
        await answer(session, abandoned_id, late)
        await answer(session, sibling_id, raw.types.Pong(msg_id=sibling_id, ping_id=2))
        assert (await asyncio.wait_for(second, 1)).ping_id == 2
        assert abandoned.value is None and not abandoned.event.is_set()
        assert isinstance(sibling.value, raw.types.Pong)
        assert session.results == {}
    finally:
        if not second.done():
            await cancel(second)


@pytest.mark.parametrize("stage", ["pack", "transport", "response"])
async def test_stop_exception_survives_cleared_or_replaced_entry(rpc, monkeypatch, stage):
    session = rpc.session
    entered, release = asyncio.Event(), asyncio.Event()
    executor = None
    if stage == "response":
        rpc.resume_wait.clear()
    if stage == "pack":
        executor = DeferredPack()
        session.connection.protocol.crypto_executor = executor
        entered = executor.entered
    elif stage == "transport":

        async def send(payload):
            entered.set()
            await release.wait()

        monkeypatch.setattr(session.connection, "send", send)
    task = asyncio.create_task(session.send(raw.functions.Ping(ping_id=1)))
    if stage == "response":
        pending = await waiting_result(rpc)
    else:
        await asyncio.wait_for(entered.wait(), 1)
        pending = rpc.results[0]
    msg_id = next(iter(session.results))
    await session.stop()
    assert session.results == {}
    assert pending.exception is not None
    # While pack/transport is still suspended, simulate another owner occupying
    # the old key. Cleanup must not remove it after reading the local stop error.
    replacement = Result()
    session.results[msg_id] = replacement
    if stage == "pack":
        assert executor is not None
        executor.finish()
    release.set()
    rpc.resume_wait.set()
    with pytest.raises(TimeoutError, match="Session stopped before an answer arrived") as caught:
        await asyncio.wait_for(task, 1)
    assert caught.value is pending.exception
    assert session.results == {msg_id: replacement}


@pytest.mark.parametrize("order", ["cancel_first", "stop_first", "response_first"])
async def test_stop_racing_response_and_cancellation_does_not_mask_cancellation(rpc, order):
    session = rpc.session
    if order == "stop_first":
        rpc.resume_wait.clear()
    task = asyncio.create_task(session.send(raw.functions.Ping(ping_id=1)))
    pending = await waiting_result(rpc)
    msg_id = next(iter(session.results))
    if order == "response_first":
        await answer(session, msg_id, raw.types.Pong(msg_id=msg_id, ping_id=1))
        result = await asyncio.wait_for(task, 1)
        await session.stop()
        assert result.ping_id == 1
    elif order == "cancel_first":
        await cancel(task)
        await session.stop()
        assert pending.exception is None
    else:
        # Hold delivery of the stop signal so cancellation has a deterministic
        # await to interrupt, rather than depending on wait_for's done/cancel race.
        await session.stop()
        await cancel(task)
        assert isinstance(pending.exception, TimeoutError)
    assert session.results == {}


@pytest.mark.parametrize("stage", ["pack", "before_attempt", "transport", "response"])
async def test_ordinary_cancellation_cleans_rpc_without_clearing_no_replay_fence(
    monkeypatch, stage
):
    f = ordinary_transport(monkeypatch, [None])
    entered = asyncio.Event()
    executor = None
    if stage == "pack":
        executor = DeferredPack()
        f.session.connection.protocol.crypto_executor = executor
        entered = executor.entered
    elif stage in ("before_attempt", "transport"):

        async def block(*args):
            entered.set()
            await asyncio.Event().wait()

        if stage == "before_attempt":
            f.hooks["before_attempt"] = block
        else:
            monkeypatch.setattr(f.session.connection, "send", block)
    else:

        async def send(payload):
            f.session.connection.sent.append(payload)
            pending = next(iter(f.session.results.values()))
            wait = pending.event.wait

            async def observed_wait():
                entered.set()
                return await wait()

            monkeypatch.setattr(pending.event, "wait", observed_wait)

        monkeypatch.setattr(f.session.connection, "send", send)

    async def publish():
        with ordinary_publication(
            f.session.client, method="SendMessage", source_ids=(101,), **f.hooks
        ) as policy:
            with pytest.raises(asyncio.CancelledError, match="cancel this RPC"):
                await f.session.invoke(ordinary_query())
            assert policy.attempted and policy.unknown and not policy.succeeded
            assert policy.native_calls == 1
            with pytest.raises(PublicationUnconfirmed, match="fresh_query_forbidden"):
                await f.session.invoke(ordinary_query())
        assert f.session.results == {}

    task = asyncio.create_task(publish())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel("cancel this RPC")
    await task
    if stage == "pack":
        assert executor is not None
        executor.finish()
        await asyncio.sleep(0)
    assert len(f.intents) == 1 and not f.receipts and not f.waits
    assert len(f.session.connection.sent) == (1 if stage == "response" else 0)


@pytest.mark.parametrize("change", ["reject", "identity", "query"])
async def test_ordinary_pre_wire_guard_failure_still_blocks_publication(monkeypatch, change):
    f = ordinary_transport(monkeypatch, [None])
    query = ordinary_query()
    assert isinstance(query, raw.functions.messages.SendMessage)

    async def before_attempt():
        if change == "reject":
            raise ValueError("fixture rejected")
        if change == "identity":
            f.session.session_id = b"changed!"
        else:
            query.message = "Changed fixture"

    f.hooks["before_attempt"] = before_attempt
    with ordinary_publication(
        f.session.client, method="SendMessage", source_ids=(101,), **f.hooks
    ) as policy:
        with pytest.raises(ValueError if change == "reject" else PublicationUnconfirmed):
            await f.session.invoke(query)
        assert policy.attempted and not policy.succeeded
        with pytest.raises(PublicationUnconfirmed, match="fresh_query_forbidden"):
            await f.session.invoke(ordinary_query())
    assert f.session.results == {}
    assert not f.session.connection.sent and not f.receipts and not f.waits


async def test_bad_salt_retry_releases_old_result_before_registering_next(rpc, monkeypatch):
    session = rpc.session
    attempts = []

    async def send(payload):
        # The old result must not survive until the recursive send returns.
        assert len(session.results) == 1
        msg_id = next(iter(session.results))
        attempts.append(msg_id)
        if len(attempts) == 1:
            body = raw.types.BadServerSalt(
                bad_msg_id=msg_id, bad_msg_seqno=0, error_code=48, new_server_salt=999
            )
            await session.handle_packet(await _server_packet(session=session, body=body))
        else:
            await answer(session, msg_id, raw.types.Pong(msg_id=msg_id, ping_id=1))

    monkeypatch.setattr(session.connection, "send", send)
    assert isinstance(await session.send(raw.functions.Ping(ping_id=1)), raw.types.Pong)
    assert len(attempts) == len(set(attempts)) == 2
    assert session.salt == 999
    assert session.results == {}

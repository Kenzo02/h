"""Native lifecycle regression cases from the independent partial-start review."""

import asyncio
import sqlite3

import pytest

from pyrogram import raw
from pyrogram.handlers import RawUpdateHandler
from pyrogram.session.session import SessionCleanupError, SessionState
from pyrogram.storage import UpdateState
from tests.unit.session.test_auxiliary_startup import AUTH_KEY, DC, wait_until
from tests.unit.session.test_startup_receiver_lifecycle import assert_stopped, native as native  # noqa: PLC0414


async def recorded_start(client, failures):
    # Python 3.10 wait_for can replace a completed task's CancelledError. Observe
    # the actual public method's exception before task retrieval.
    try:
        return await client.start()
    except BaseException as error:
        failures.append(error)
        raise


@pytest.mark.parametrize("fault", ["cancel", "error"])
@pytest.mark.parametrize("startup", ["error", "cancel", "late-cancel"])
async def test_partial_dispatcher_drains_every_worker_before_storage_close(
    native, monkeypatch, fault, startup
):
    c = native.client
    c.workers = 2
    primary = ValueError("offline gap recovery failed")
    worker_error = RuntimeError("offline queue failed")
    native.options = [
        {
            "fail_query": raw.functions.updates.GetDifference,
            "failure": primary,
            "block_query": raw.functions.updates.GetDifference,
        }
    ]
    entered = []
    first_release, release = asyncio.Event(), asyncio.Event()

    async def handler(client, update, users, chats):
        entered.append(asyncio.current_task())
        await (first_release if update.user_id == 1 else release).wait()
        assert await client.storage.api_id() == 1

    c.add_handler(RawUpdateHandler(handler))
    failures = []
    starting = asyncio.create_task(recorded_start(c, failures))
    workers = []
    try:
        await wait_until(
            lambda: native.transports and native.transports[0].request_entered.is_set()
        )
        session, wire = c.session, native.transports[0]
        native.owned.append(session)
        workers = list(c.dispatcher.handler_worker_tasks)
        for uid in (1, 2):
            c.dispatcher.enqueue(
                raw.types.UpdateUserStatus(user_id=uid, status=raw.types.UserStatusEmpty()),
                users={},
                chats={},
            )
        await wait_until(lambda: len(entered) == 2)
        if fault == "cancel":
            workers[0].cancel("offline worker cancelled")
        else:
            get = c.dispatcher.updates_queue.get

            async def broken_get():
                if asyncio.current_task() is workers[0]:
                    raise worker_error
                return await get()

            monkeypatch.setattr(c.dispatcher.updates_queue, "get", broken_get)
            first_release.set()
        await wait_until(workers[0].done)
        if startup == "cancel":
            starting.cancel("original initialize cancellation")
        else:
            wire.request_release.set()
        await wait_until(lambda: not c.dispatcher._accepting_updates)
        await asyncio.sleep(0)
        assert not starting.done() and not workers[1].done()
        assert await c.storage.api_id() == 1
        if startup == "late-cancel":
            starting.cancel("original initialize cancellation")
            await asyncio.sleep(0)
            assert not starting.done() and await c.storage.api_id() == 1
        if startup == "cancel":
            starting.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not starting.done() and await c.storage.api_id() == 1
        release.set()
        with pytest.raises(ValueError if startup == "error" else asyncio.CancelledError) as caught:
            await asyncio.wait_for(starting, 2)
        if startup == "error":
            assert caught.value is primary
            assert isinstance(primary.__cause__, SessionCleanupError)
            causes = [e for _, e in primary.__cause__.failures]
            assert (
                any(isinstance(e, asyncio.CancelledError) for e in causes)
                if fault == "cancel"
                else worker_error in causes
            )
        else:
            assert failures[0].args == ("original initialize cancellation",)
        assert all(t.done() for t in workers)
        assert c.dispatcher.groups  # Retry preserves the user's handlers.
        assert wire.requests.count(raw.functions.updates.GetDifference.QUALNAME) == 1
        assert_stopped(native, session)
    finally:
        first_release.set()
        release.set()
        for wire in native.transports:
            wire.request_release.set()
        await asyncio.gather(starting, *workers, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("reject_media_close", [False, True])
async def test_partial_initialize_rolls_back_media_and_auxiliary_sessions(
    native, monkeypatch, cancel, reject_media_close
):
    c = native.client
    primary = ValueError("offline gap recovery failed")
    secondary = RuntimeError("offline media close rejected")
    native.options = [
        {
            "fail_query": raw.functions.updates.GetDifference,
            "failure": primary,
            "block_query": raw.functions.updates.GetDifference,
        }
    ]

    async def auth_key(_self):
        return AUTH_KEY

    monkeypatch.setattr("pyrogram.client.Auth.create", auth_key)
    created = []
    originals = []

    async def start_handler(client):
        for dc_id, media in ((DC, True), (4, False), (3, False)):
            session = await client.get_session(
                dc_id,
                is_media=media,
                server_address="127.0.0.1",
                port=443,
                export_authorization=False,
            )
            created.append(session)
            native.owned.append(session)
        if reject_media_close:
            originals.append((created[0].connection, created[0].connection.close))

            async def reject():
                raise secondary

            monkeypatch.setattr(created[0].connection, "close", reject)

    c.start_handler = start_handler
    failures = []
    starting = asyncio.create_task(recorded_start(c, failures))
    try:
        await wait_until(
            lambda: native.transports and native.transports[0].request_entered.is_set()
        )
        main, wire = c.session, native.transports[0]
        native.owned.append(main)
        if cancel:
            starting.cancel("original auxiliary startup cancellation")
        else:
            wire.request_release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError) as caught:
            await asyncio.wait_for(starting, 2)
        assert (
            failures[0].args == ("original auxiliary startup cancellation",)
            if cancel
            else caught.value is primary
        )
        assert not c.is_connected and not c.is_initialized and c.session is None
        assert not c.sessions and not c.dispatcher.handler_worker_tasks
        assert all(not s._has_live_workers() and s._must_stay_stopped for s in [main, *created])
        assert all(s.state is SessionState.STOPPED for s in [main, *created[1:]])
        if reject_media_close:
            assert c.media_sessions == {DC: created[0]}
            assert created[0].state is SessionState.STOP_FAILED
            assert any(e is secondary for _, e in failures[0].__cause__.failures)
            assert created[0].connection.close_calls == 0
        else:
            assert not c.media_sessions
            assert_stopped(native, main)
        with pytest.raises(sqlite3.ProgrammingError):
            c.storage.conn.execute("SELECT 1")
        assert wire.requests.count(raw.functions.updates.GetDifference.QUALNAME) == 1
    finally:
        for wire in native.transports:
            wire.request_release.set()
        for wire, close in originals:
            monkeypatch.setattr(wire, "close", close)
        await asyncio.gather(starting, return_exceptions=True)


async def test_initialize_and_main_cleanup_failures_both_remain_attached(native, monkeypatch):
    c = native.client
    primary = ValueError("offline gap recovery failed")
    media_error, main_error = RuntimeError("media close failed"), RuntimeError("main close failed")
    native.options = [{"fail_query": raw.functions.updates.GetDifference, "failure": primary}]
    originals = []
    created = []

    async def start_handler(client):
        media = await client.get_session(
            DC, is_media=True, export_authorization=False, server_address="127.0.0.1", port=443
        )
        created.append(media)
        native.owned.append(media)
        for session, failure in ((media, media_error), (client.session, main_error)):
            originals.append((session.connection, session.connection.close))

            async def reject(error=failure):
                raise error

            monkeypatch.setattr(session.connection, "close", reject)

    c.start_handler = start_handler
    try:
        with pytest.raises(ValueError) as caught:
            await c.start()
        assert caught.value is primary
        assert isinstance(primary.__cause__, SessionCleanupError)
        causes = [error for _, error in primary.__cause__.failures]
        assert media_error in causes and main_error in causes
        assert c.media_sessions == {DC: created[0]} and c.session is not None
        assert all(
            s.state is SessionState.STOP_FAILED and not s._has_live_workers()
            for s in (c.session, created[0])
        )
        assert not c.dispatcher.handler_worker_tasks
        with pytest.raises(sqlite3.ProgrammingError):
            c.storage.conn.execute("SELECT 1")
    finally:
        for wire, close in originals:
            monkeypatch.setattr(wire, "close", close)


async def test_receiver_error_survives_restart_stop_without_request_replay(native, monkeypatch):
    c = native.client
    await c.start()
    session, wire = c.session, c.session.connection
    native.owned.append(session)
    primary = ValueError("offline receiver failed")
    wire.recv_failure = primary

    async def fail_reply(*args):
        await wire.incoming.put(None)

    monkeypatch.setattr(wire, "reply", fail_reply)
    with pytest.raises(ValueError) as caught:
        await session.invoke(raw.functions.help.GetConfig(), retries=3, retry_delay=0, timeout=0.5)
    assert caught.value is primary
    assert wire.requests.count(raw.functions.help.GetConfig.QUALNAME) == 1
    await asyncio.wait_for(session.restart_task, 2)
    assert len(native.transports) == 2
    assert all(
        raw.functions.help.GetConfig.QUALNAME not in w.requests for w in native.transports[1:]
    )
    assert session.is_started.is_set() and not session.recv_task.done()


@pytest.mark.parametrize("answer", ["response", "error", "unresolved"])
async def test_terminal_stop_only_fails_unresolved_results(native, monkeypatch, answer):
    c = native.client
    await c.start()
    session, wire = c.session, c.session.connection
    native.owned.append(session)
    reply = wire.reply
    primary = ValueError("offline completed result error")
    values = []

    async def stop_before_sender_resumes(response, session_id):
        if answer == "response":
            await reply(response, session_id)
            await wait_until(lambda: any(r.event.is_set() for r in session.results.values()))
            values.append(next(r.value for r in session.results.values() if r.event.is_set()))
        elif answer == "error":
            result = next(iter(session.results.values()))
            result.exception = primary
            result.event.set()
        await session.stop()

    monkeypatch.setattr(wire, "reply", stop_before_sender_resumes)
    if answer == "response":
        assert await session.send(raw.functions.help.GetConfig(), timeout=0.5) is values[0]
    else:
        with pytest.raises(ValueError if answer == "error" else TimeoutError) as caught:
            await session.send(raw.functions.help.GetConfig(), timeout=0.5)
        if answer == "error":
            assert caught.value is primary
        else:
            assert str(caught.value) == "Session stopped before an answer arrived"
    assert wire.requests.count(raw.functions.help.GetConfig.QUALNAME) == 1
    assert not session.results and session.state is SessionState.STOPPED
    session.schedule_restart("late EOF")
    assert session.restart_task is None and len(native.transports) == 1


@pytest.mark.parametrize("source", ["external", "provider"])
async def test_unowned_receiver_cancellation_clears_readiness_and_recovers(native, source):
    c = native.client
    await c.start()
    session, wire = c.session, c.session.connection
    native.owned.append(session)
    receiver = session.recv_task
    if source == "external":
        receiver.cancel("offline receiver cancellation")
    else:
        wire.recv_failure = asyncio.CancelledError("offline provider cancellation")
        await wire.incoming.put(None)
    await asyncio.gather(receiver, return_exceptions=True)
    assert not session.is_started.is_set()
    assert session.restart_task is not None
    await asyncio.wait_for(session.restart_task, 2)
    assert session.is_started.is_set() and not session.recv_task.done()
    assert isinstance(await session.invoke(raw.functions.help.GetConfig()), raw.types.Config)
    await c.stop()
    assert_stopped(native, session)


@pytest.mark.parametrize("phase", ["load", "connect", "post-connect"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_connect_failure_rolls_back_storage_and_sessions(native, monkeypatch, phase, cancel):
    c = native.client
    primary = ValueError("offline connect startup failure")
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail():
        entered.set()
        if cancel:
            await release.wait()
        raise primary

    if phase == "load":
        # Execute actual load_session/open, then fail at its first storage read.
        await c.storage.close()
        monkeypatch.setattr(c, "load_session", type(c).load_session.__get__(c))
        monkeypatch.setattr(c.storage, "test_mode", fail)
    elif phase == "connect":
        native.options = [{"mode": "block" if cancel else "error"}]
    else:
        user_id = c.storage.user_id

        async def failing_user_id(*args):
            if c.is_connected:
                await fail()
            return await user_id(*args)

        monkeypatch.setattr(c.storage, "user_id", failing_user_id)
    failures = []
    starting = asyncio.create_task(recorded_start(c, failures))
    try:
        if cancel:
            if phase == "connect":
                await wait_until(
                    lambda: native.transports and native.transports[0].entered.is_set()
                )
            else:
                await entered.wait()
            starting.cancel("original connect cancellation")
        with pytest.raises(
            asyncio.CancelledError
            if cancel
            else ConnectionError
            if phase == "connect"
            else ValueError
        ) as caught:
            await asyncio.wait_for(starting, 2)
        if cancel:
            assert failures[0].args == ("original connect cancellation",)
        elif phase != "connect":
            assert caught.value is primary
        assert not c.is_connected and c.session is None
        assert not c.sessions and not c.media_sessions and not c.dispatcher.handler_worker_tasks
        assert all(w.closed for w in native.transports)
        with pytest.raises(sqlite3.ProgrammingError):
            c.storage.conn.execute("SELECT 1")
    finally:
        release.set()
        for wire in native.transports:
            wire.release.set()
        await asyncio.gather(starting, return_exceptions=True)


async def test_initialize_rollback_keeps_connected_owner_and_allows_retry(native):
    c = native.client
    await c.connect()
    await c.storage.set_update_state(UpdateState(0, 1, 0, 1, 1))
    main, wire = c.session, c.session.connection
    primary = ValueError("offline direct initialize failed")
    wire.fail_query, wire.failure = raw.functions.updates.GetDifference, primary
    created = []

    async def start_handler(client):
        extra = await client.get_session(
            DC, is_media=True, export_authorization=False, server_address="127.0.0.1", port=443
        )
        created.append(extra)
        native.owned.append(extra)

    c.start_handler = start_handler
    with pytest.raises(ValueError) as caught:
        await c.initialize()
    assert caught.value is primary
    assert c.is_connected and not c.is_initialized and c.session is main
    assert main.is_started.is_set() and await c.storage.api_id() == 1
    assert not c.media_sessions and not c.dispatcher.handler_worker_tasks
    assert created[0].state is SessionState.STOPPED
    wire.fail_query = None
    await c.initialize()
    assert c.is_initialized and len(created) == 2
    assert created[1] is not created[0] and created[1].is_started.is_set()
    assert wire.requests.count(raw.functions.updates.GetDifference.QUALNAME) == 2
    await c.stop()
    assert_stopped(native, main)


async def test_public_terminal_stop_drains_auxiliaries_after_dispatcher_worker_failure(native):
    c = native.client
    c.workers = 2
    await c.start()
    main = c.session
    extra = await c.get_session(
        DC, is_media=True, export_authorization=False, server_address="127.0.0.1", port=443
    )
    native.owned.extend([main, extra])
    entered = []
    release = asyncio.Event()

    async def handler(client, update, users, chats):
        entered.append(asyncio.current_task())
        await release.wait()
        assert await client.storage.api_id() == 1

    c.add_handler(RawUpdateHandler(handler))
    workers = list(c.dispatcher.handler_worker_tasks)
    for uid in (1, 2):
        c.dispatcher.enqueue(
            raw.types.UpdateUserStatus(user_id=uid, status=raw.types.UserStatusEmpty()),
            users={},
            chats={},
        )
    await wait_until(lambda: len(entered) == 2)
    workers[0].cancel("offline dispatcher failure")
    await wait_until(workers[0].done)
    stopping = asyncio.create_task(c.stop())
    try:
        await wait_until(lambda: not c.dispatcher._accepting_updates)
        await asyncio.sleep(0)
        assert not stopping.done() and not workers[1].done()
        assert await c.storage.api_id() == 1
        release.set()
        with pytest.raises(SessionCleanupError) as caught:
            await asyncio.wait_for(stopping, 2)
        assert any(isinstance(e, asyncio.CancelledError) for _, e in caught.value.failures)
        assert all(t.done() for t in workers)
        assert extra.state is SessionState.STOPPED and not c.media_sessions
        assert_stopped(native, main)
        count = len(native.transports)
        for session in (main, extra):
            session.schedule_restart("late EOF")
            await session.start()
        assert len(native.transports) == count
    finally:
        release.set()
        await asyncio.gather(stopping, *workers, return_exceptions=True)


async def test_duplicate_start_does_not_tear_down_existing_client(native):
    c = native.client
    await c.start()
    session = c.session
    with pytest.raises(ConnectionError, match="already connected"):
        await c.start()
    assert c.is_connected and c.is_initialized and c.session is session
    assert session.is_started.is_set() and await c.storage.api_id() == 1
    assert (await c.get_me()).id == 1
    await c.stop()
    assert_stopped(native, session)

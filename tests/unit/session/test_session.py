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

import asyncio
from concurrent.futures import Executor
from hashlib import sha1, sha256
from io import BytesIO
from os import urandom
from typing import Final

import pytest

import pyrogram
from pyrogram import raw
from pyrogram.crypto import aes, mtproto
from pyrogram.raw.core import FutureSalt, FutureSalts, Long, Message, MsgContainer, TLObject
from pyrogram.session.session import Result, Session, SessionState

_DC_ID: Final[int] = 2
_PORT: Final[int] = 443
_AUTH_KEY: Final[bytes] = bytes(256)
_PACKET: Final[bytes] = b"one packet"

# The salt the session holds before the server offers a new one, and the one it offers.
_STALE_SALT: Final[int] = 111
_NEW_SALT: Final[int] = 999

# The two salts of a pool: the one in use, and the one that takes over from it.
_CURRENT_SALT: Final[int] = 222
_NEXT_SALT: Final[int] = 333

# How long the server gives a salt, and how much of it is left when a test starts.
_SALT_LIFETIME: Final[int] = 30 * 60
_SALT_LEFT: Final[int] = 30

# `bad_server_salt` error code: "incorrect server salt".
#  https://core.telegram.org/mtproto/service_messages_about_messages#notice-of-ignored-error-message
_INCORRECT_SERVER_SALT: Final[int] = 48

# Long enough that a `stop()` which does not wait would have returned several times
#  over, short enough to keep the suite quick.
_NOT_DONE_TIMEOUT: Final[float] = 0.1

# What `Session.STOP_TIMEOUT` is replaced with, so a test of the cancelling path does
#  not sit through the real grace period.
_SHORT_STOP_TIMEOUT: Final[float] = 0.05


class StubProtocol:
    def __init__(self) -> None:
        # `handle_packet` unpacks in this executor; `None` means the loop default one.
        self.crypto_executor: Executor | None = None


class StubConnection:
    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.packets = [_PACKET]
        self.sent: list[bytes] = []
        self.protocol = StubProtocol()

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> bytes | None:
        if self.packets:
            return self.packets.pop()

        await self.closed.wait()
        return None

    async def close(self) -> None:
        self.closed.set()


def _session() -> Session:
    client = pyrogram.Client("test", api_id=1, api_hash="0" * 32, in_memory=True)

    return Session(client, _DC_ID, "127.0.0.1", _PORT, _AUTH_KEY, test_mode=True)


def _started_session() -> Session:
    session = _session()

    session.connection = StubConnection()
    session._state = SessionState.STARTED
    session.is_started.set()

    return session


def _pack_as_server(message: Message, *, session_id: bytes, auth_key: bytes) -> bytes:
    """`pyrogram.crypto.mtproto.pack`, in the direction the server writes.

    The outer salt is the sender's own and `unpack` skips it, so it is left at zero.
    """
    data: bytes = Long(0) + session_id + message.write()
    padding = urandom(-(len(data) + 12) % 16 + 12)

    # 96 = 88 + 8, the offset an incoming message keys on.
    msg_key = sha256(auth_key[96 : 96 + 32] + data + padding).digest()[8:24]
    aes_key, aes_iv = mtproto.kdf(auth_key, msg_key, False)

    return (
        sha1(auth_key).digest()[-8:] + msg_key + aes.ige256_encrypt(data + padding, aes_key, aes_iv)
    )


async def _server_packet(*, session: Session, body: TLObject) -> bytes:
    """Pack a body into the packet a server would have written"""
    # A server message identity is odd, and the factory allocates client ones.
    message = Message(
        body,
        await session.msg_factory.allocate_message_identity() + 1,
        1,
        len(body),
    )

    return _pack_as_server(
        message,
        session_id=session.session_id,
        auth_key=session.auth_key,
    )


async def _bad_server_salt_packet(*, session: Session, bad_msg_id: int) -> bytes:
    body = raw.types.BadServerSalt(
        bad_msg_id=bad_msg_id,
        bad_msg_seqno=0,
        error_code=_INCORRECT_SERVER_SALT,
        new_server_salt=_NEW_SALT,
    )

    return await _server_packet(
        session=session,
        body=body,
    )


def _last_sent_salt(session: Session) -> int:
    """Read back the salt the session packed its last outgoing message with"""
    connection = session.connection
    assert isinstance(connection, StubConnection)

    # `mtproto.pack` writes `auth_key_id` (8 bytes) and `msg_key` (16), then the
    #  encrypted block, which opens on the salt.
    payload = connection.sent[-1]
    aes_key, aes_iv = mtproto.kdf(session.auth_key, payload[8:24], True)

    return Long.read(BytesIO(aes.ige256_decrypt(payload[24:], aes_key, aes_iv)))


async def _future_salts_packet(
    *,
    session: Session,
    req_msg_id: int,
    salts: list[FutureSalt],
) -> bytes:
    body = FutureSalts(
        req_msg_id=req_msg_id,
        now=int(session.client.server_time),
        salts=salts,
    )

    return await _server_packet(
        session=session,
        body=body,
    )


async def _take_future_salts(*, session: Session, salts: list[FutureSalt]) -> None:
    """Answer the `GetFutureSalts` the session sends, the way the server would"""
    requesting = asyncio.ensure_future(session._update_future_salts())

    while not session.results and not requesting.done():
        await asyncio.sleep(0)

    assert session.results, "the session asked for no future salts"

    await session.handle_packet(
        await _future_salts_packet(
            session=session,
            req_msg_id=next(iter(session.results)),
            salts=salts,
        ),
    )

    await requesting


def _pool_expiring_now(session: Session) -> list[FutureSalt]:
    """A pool of two: a salt with seconds left, and the one that succeeds it"""
    now = int(session.client.server_time)

    return [
        FutureSalt(
            valid_since=now - _SALT_LIFETIME,
            valid_until=now + _SALT_LEFT,
            salt=_CURRENT_SALT,
        ),
        FutureSalt(
            valid_since=now + _SALT_LEFT,
            valid_until=now + _SALT_LEFT + _SALT_LIFETIME,
            salt=_NEXT_SALT,
        ),
    ]


def _recorded_future_salts_requests(session: Session, *, valid_for: int) -> list[TLObject]:
    """Answer every `GetFutureSalts` where it is sent, and collect what was asked"""
    requests: list[TLObject] = []

    async def send(
        data: TLObject,
        *,
        wait_response: bool = True,
        timeout: float = Session.WAIT_TIMEOUT,
    ) -> FutureSalts:
        requests.append(data)

        # The second entry is what keeps the pool from emptying as the first is
        #  promoted, so the threshold is what decides the next request rather than
        #  the pool being empty.
        now = int(session.client.server_time)
        expires_at: int = now + valid_for
        salts = [
            FutureSalt(
                valid_since=now - _SALT_LIFETIME,
                valid_until=expires_at,
                salt=_CURRENT_SALT,
            ),
            FutureSalt(
                valid_since=expires_at,
                valid_until=expires_at + _SALT_LIFETIME,
                salt=_NEXT_SALT,
            ),
        ]

        return FutureSalts(
            req_msg_id=0,
            now=now,
            salts=salts,
        )

    session.send = send

    return requests


async def test_stop_waits_for_the_packet_it_is_still_handling() -> None:
    session = _started_session()

    release = asyncio.Event()
    handled: bool = False

    async def handle_packet(packet: bytes) -> None:
        nonlocal handled

        await release.wait()
        handled = True

    session.handle_packet = handle_packet
    session.recv_task = asyncio.create_task(session.recv_worker())

    await asyncio.sleep(0)

    stopping = asyncio.ensure_future(session.stop())

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(stopping), _NOT_DONE_TIMEOUT)

    assert not handled

    release.set()
    await stopping

    assert handled
    assert session.pending_tasks == set()


async def test_stop_cancels_a_packet_that_will_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Session, "STOP_TIMEOUT", _SHORT_STOP_TIMEOUT)

    session = _started_session()

    async def handle_packet(packet: bytes) -> None:
        await asyncio.Event().wait()

    session.handle_packet = handle_packet
    session.recv_task = asyncio.create_task(session.recv_worker())

    await asyncio.sleep(0)

    handling = next(iter(session.pending_tasks))

    await session.stop()

    assert handling.cancelled()
    assert session.pending_tasks == set()


async def test_a_finished_task_leaves_the_pending_set() -> None:
    session = _started_session()

    task = session._create_tracked_task(asyncio.sleep(0))

    assert session.pending_tasks == {task}

    await task
    await asyncio.sleep(0)

    assert session.pending_tasks == set()


async def test_a_restart_queued_before_stop_does_not_reconnect() -> None:
    session = _started_session()

    started: bool = False

    async def start() -> None:
        nonlocal started

        started = True

    session.start = start

    # The task is only scheduled here: it runs once the loop is yielded to, which is
    #  after the stop below, and that is the order the client shuts down in.
    restarting = asyncio.create_task(session.restart())

    await session.stop()
    await restarting

    assert not started
    assert session.state is SessionState.STOPPED


async def test_a_restart_already_starting_is_stopped_again() -> None:
    session = _started_session()

    starting = asyncio.Event()
    release = asyncio.Event()

    async def start() -> None:
        starting.set()
        await release.wait()

        session._state = SessionState.STARTED
        session.is_started.set()

    session.start = start
    restarting = asyncio.create_task(session.restart())

    await starting.wait()
    await session.stop()

    release.set()
    await restarting

    assert session.state is SessionState.STOPPED
    assert not session.is_started.is_set()


async def test_stop_fails_the_request_still_waiting_for_its_answer() -> None:
    session = _started_session()

    sending = asyncio.ensure_future(session.send(raw.functions.Ping(ping_id=0)))

    while not session.results:
        await asyncio.sleep(0)

    await session.stop()

    with pytest.raises(TimeoutError, match="stopped"):
        await asyncio.wait_for(sending, _NOT_DONE_TIMEOUT)

    assert session.results == {}


async def test_stop_drops_the_acks_owed_to_the_closed_connection() -> None:
    session = _started_session()

    # A server message identity is odd, and its ack was never flushed.
    session.pending_acks.add(await session.msg_factory.allocate_message_identity() + 1)

    await session.stop()

    assert session.pending_acks == set()


async def test_a_bad_server_salt_nobody_awaits_still_updates_the_salt() -> None:
    session = _started_session()
    session.salt = _STALE_SALT

    # `ping_worker` sends with `wait_response=False`, so nothing registers a `Result`.
    ping_msg_id = await session.msg_factory.allocate_message_identity()

    await session.handle_packet(
        await _bad_server_salt_packet(
            session=session,
            bad_msg_id=ping_msg_id,
        ),
    )

    assert ping_msg_id not in session.results
    assert session.salt == _NEW_SALT


async def test_a_bad_server_salt_still_resolves_the_call_that_waits_for_it() -> None:
    session = _started_session()
    session.salt = _STALE_SALT

    awaited_msg_id = await session.msg_factory.allocate_message_identity()
    session.results[awaited_msg_id] = Result()

    await session.handle_packet(
        await _bad_server_salt_packet(
            session=session,
            bad_msg_id=awaited_msg_id,
        ),
    )

    assert session.results[awaited_msg_id].event.is_set()
    assert session.salt == _NEW_SALT


async def test_the_session_packs_with_the_pooled_salt_once_the_current_one_expires() -> None:
    session = _started_session()

    await _take_future_salts(
        session=session,
        salts=_pool_expiring_now(session),
    )

    await session.send(raw.functions.Ping(ping_id=0), wait_response=False)

    assert _last_sent_salt(session) == _CURRENT_SALT

    # Past the first salt's `valid_until`, and nothing has sent a `BadServerSalt`.
    session.client._server_time_offset += _SALT_LEFT + 1

    await session.send(raw.functions.Ping(ping_id=0), wait_response=False)

    assert _last_sent_salt(session) == _NEXT_SALT
    assert session.results == {}


async def test_future_salts_are_not_asked_for_twice_within_the_minute() -> None:
    session = _started_session()

    # A salt already inside the threshold, so the interval is the one thing left
    #  that can stop the second request.
    requests = _recorded_future_salts_requests(session, valid_for=_SALT_LEFT)

    await session._update_future_salts()
    await session._update_future_salts()

    assert len(requests) == 1


async def test_future_salts_are_asked_for_before_the_current_salt_expires() -> None:
    session = _started_session()

    # A salt that outlives the interval below, but not by the whole threshold.
    requests = _recorded_future_salts_requests(
        session,
        valid_for=Session.FUTURE_SALTS_INTERVAL + _SALT_LEFT,
    )

    await session._update_future_salts()

    session.client._server_time_offset += Session.FUTURE_SALTS_INTERVAL + 1

    await session._update_future_salts()

    assert len(requests) == 2
    assert session.salt_valid_until > session.client.server_time


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(SessionState.STOPPED, id="never-started"),
        pytest.param(SessionState.STARTING, id="left-starting-by-a-failed-start"),
    ],
)
async def test_invoke_on_a_session_that_is_not_running_raises(
    state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Short enough to keep the suite quick, and `invoke()` waits it out in full
    #  before it gives up on a session that is not running.
    monkeypatch.setattr(Session, "WAIT_TIMEOUT", 0.05)

    session = _session()
    session._state = state

    with pytest.raises(TimeoutError) as raised:
        await session.invoke(raw.functions.help.GetConfig())

    message = str(raised.value)

    assert 'invoke "help.GetConfig"' in message

    # `Session.__str__` carries the state, so this pins the message on both parameters.
    assert str(session) in message


def _client_message(session: Session, payload: bytes) -> Message:
    aes_key, aes_iv = mtproto.kdf(session.auth_key, payload[8:24], True)
    plain = BytesIO(aes.ige256_decrypt(payload[24:], aes_key, aes_iv))
    plain.read(16)  # salt and session id
    return Message.read(plain)


@pytest.mark.parametrize("history", ["recent", "stored"])
@pytest.mark.parametrize("duplicate_count,pending", [(1, False), (30, True)])
async def test_known_duplicates_ack_without_restart_or_hiding_fresh_pong(
    history: str, duplicate_count: int, pending: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _started_session()
    monkeypatch.setattr(Session, "ACKS_THRESHOLD", 1)
    restarted: list[str] = []
    monkeypatch.setattr(session, "schedule_restart", restarted.append)
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    duplicate_id = await session.msg_factory.allocate_message_identity() + 1
    fresh_id = await session.msg_factory.allocate_message_identity() + 1
    getattr(session, f"{history}_msg_ids").append(duplicate_id)
    if pending:
        session.pending_acks.add(duplicate_id)
    duplicate = raw.types.NewSessionCreated(first_msg_id=1, unique_id=2, server_salt=3)
    pong = raw.types.Pong(msg_id=request, ping_id=7)
    messages = [Message(duplicate, duplicate_id, 1, len(duplicate))] * duplicate_count
    messages.append(Message(pong, fresh_id, 2, len(pong)))

    await session.handle_packet(await _server_packet(session=session, body=MsgContainer(messages)))

    assert restarted == []
    assert session.results[request].event.is_set()
    assert session.results[request].value == pong
    assert session.ignore_count == 0
    assert duplicate_id in getattr(session, f"{history}_msg_ids")
    assert isinstance(session.connection, StubConnection)
    assert len(session.connection.sent) == 1
    ack = _client_message(session, session.connection.sent[0]).body
    assert isinstance(ack, raw.types.MsgsAck)
    assert ack.msg_ids == [duplicate_id]
    assert session.pending_acks == set()


async def test_matched_bad_msg_code16_recovers_clock_with_stored_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pyrogram.client.time.time", lambda: 1_700_000_000.0)
    session = _started_session()
    session.client._server_time_offset = -60
    session.stored_msg_ids = [await session.msg_factory.allocate_message_identity() - 3]
    server_id = (1_700_000_000 << 32) + 1
    assert isinstance(session.connection, StubConnection)

    async def answer(payload: bytes) -> None:
        request = _client_message(session, payload)
        body = raw.types.BadMsgNotification(
            bad_msg_id=request.msg_id, bad_msg_seqno=request.seq_no, error_code=16
        )
        await session.handle_packet(
            _pack_as_server(
                Message(body, server_id, 0, len(body)),
                session_id=session.session_id,
                auth_key=session.auth_key,
            )
        )

    monkeypatch.setattr(session.connection, "send", answer)
    result = await session.send(raw.functions.Ping(ping_id=0), timeout=0.1)

    assert isinstance(result, raw.types.BadMsgNotification)
    assert result.error_code == 16
    assert session.client._server_time_offset == 0
    assert server_id in session.stored_msg_ids
    assert session.ignore_count == 0
    assert session.results == {}
    assert await session.msg_factory.allocate_message_identity() > result.bad_msg_id


async def test_fresh_seq0_bootstraps_clock_without_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pyrogram.client.time.time", lambda: 1_700_000_000.0)
    session = _started_session()
    session.client._server_time_offset = -60
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    pong = raw.types.Pong(msg_id=request, ping_id=0)
    server_id = (1_700_000_000 << 32) + 1

    await session.handle_packet(
        _pack_as_server(
            Message(pong, server_id, 0, len(pong)),
            session_id=session.session_id,
            auth_key=session.auth_key,
        )
    )

    assert session.client._server_time_offset == 0
    assert session.results[request].event.is_set()
    assert session.results[request].value == pong
    assert session.stored_msg_ids == [server_id]


@pytest.mark.parametrize("detailed_type", [raw.types.MsgDetailedInfo, raw.types.MsgNewDetailedInfo])
async def test_detailed_info_pending_ack_does_not_hide_unprocessed_answer(detailed_type) -> None:
    session = _started_session()
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    answer_id = await session.msg_factory.allocate_message_identity() + 1
    session.stored_msg_ids = [answer_id - 4]
    pong = raw.types.Pong(msg_id=request, ping_id=7)
    answer = raw.types.RpcResult(req_msg_id=request, result=pong)
    kwargs = {"msg_id": request} if detailed_type is raw.types.MsgDetailedInfo else {}
    detail = detailed_type(answer_msg_id=answer_id, bytes=len(answer), status=0, **kwargs)
    await session.handle_packet(await _server_packet(session=session, body=detail))
    assert answer_id in session.pending_acks
    assert answer_id not in session.stored_msg_ids

    await session.handle_packet(
        await _server_packet(
            session=session,
            body=MsgContainer([Message(answer, answer_id, 1, len(answer))]),
        )
    )

    assert session.results[request].event.is_set()
    assert session.results[request].value == pong
    assert answer_id in session.stored_msg_ids
    assert session.ignore_count == 0


@pytest.mark.parametrize("rejection", ["recent", "stored", "below-min", "too-old", "too-new"])
async def test_rejected_container_item_does_not_hide_a_fresh_reply(rejection: str) -> None:
    session = _started_session()
    old_request = await session.msg_factory.allocate_message_identity()
    new_request = await session.msg_factory.allocate_message_identity()
    session.results[old_request] = Result()
    session.results[new_request] = Result()
    old_id = await session.msg_factory.allocate_message_identity() + 1
    fresh_id = await session.msg_factory.allocate_message_identity() + 1
    if rejection == "recent":
        session.recent_msg_ids = [old_id]
    elif rejection == "stored":
        session.stored_msg_ids = [old_id]
    elif rejection == "below-min":
        old_id = fresh_id - 8
        session.stored_msg_ids = [old_id + 4]
    elif rejection == "too-old":
        old_id -= 301 << 32
        session.stored_msg_ids = [old_id - 4]
    else:
        old_id += 31 << 32
        session.stored_msg_ids = [fresh_id - 4]
    old_reply = raw.types.Pong(msg_id=old_request, ping_id=1)
    fresh_reply = raw.types.Pong(msg_id=new_request, ping_id=2)
    container = MsgContainer(
        [
            Message(old_reply, old_id, 1, len(old_reply)),
            Message(fresh_reply, fresh_id, 1, len(fresh_reply)),
        ]
    )

    await session.handle_packet(await _server_packet(session=session, body=container))

    assert not session.results[old_request].event.is_set()
    assert session.results[new_request].event.is_set()
    assert session.results[new_request].value.ping_id == 2
    expected_acks = {old_id, fresh_id} if rejection in ("recent", "stored") else {fresh_id}
    assert session.pending_acks == expected_acks


async def test_reconnect_container_keeps_pong_after_multiple_replayed_items() -> None:
    session = _started_session()
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    bodies = [
        raw.types.NewSessionCreated(first_msg_id=1, unique_id=2, server_salt=3),
        raw.types.RpcResult(req_msg_id=4, result=raw.types.Pong(msg_id=4, ping_id=1)),
        raw.types.Pong(msg_id=request, ping_id=0),
    ]
    messages = [
        Message(body, await session.msg_factory.allocate_message_identity() + 1, 1, len(body))
        for body in bodies
    ]
    session.recent_msg_ids = [item.msg_id for item in messages[:2]]

    await session.handle_packet(
        await _server_packet(session=session, body=MsgContainer(messages)),
    )

    assert session.results[request].event.is_set()
    assert session.results[request].value.ping_id == 0
    assert session.pending_tasks == set()


async def test_rejected_only_container_never_resolves_a_request() -> None:
    session = _started_session()
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    body = raw.types.Pong(msg_id=request, ping_id=0)
    message = Message(body, await session.msg_factory.allocate_message_identity() + 1, 1, len(body))
    session.recent_msg_ids = [message.msg_id]

    await session.handle_packet(
        await _server_packet(session=session, body=MsgContainer([message])),
    )

    assert not session.results[request].event.is_set()
    assert session.pending_tasks == set()


async def test_rejection_threshold_still_restarts_and_stops_packet_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _started_session()
    session.ignore_count = Session.MAX_CONSECUTIVE_IGNORED - 1
    restarted: list[str] = []
    monkeypatch.setattr(session, "schedule_restart", restarted.append)
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    body = raw.types.Pong(msg_id=request, ping_id=0)
    fresh_id = await session.msg_factory.allocate_message_identity() + 1
    old_id = fresh_id - 8
    session.stored_msg_ids = [old_id + 4]

    await session.handle_packet(
        await _server_packet(
            session=session,
            body=MsgContainer(
                [
                    Message(body, old_id, 1, len(body)),
                    Message(body, fresh_id, 1, len(body)),
                ]
            ),
        ),
    )

    assert len(restarted) == 1
    assert not session.results[request].event.is_set()


@pytest.mark.parametrize("same_container", [True, False])
async def test_recent_even_seq_repeated_cannot_resolve_a_request(same_container: bool) -> None:
    session = _started_session()
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    body = raw.types.Pong(msg_id=request, ping_id=9)
    old_id = await session.msg_factory.allocate_message_identity() + 1
    session.recent_msg_ids = [old_id]
    message = Message(body, old_id, 0, len(body))
    if same_container:
        await session.handle_packet(
            await _server_packet(session=session, body=MsgContainer([message, message])),
        )
    else:
        for _ in range(2):
            await session.handle_packet(
                await _server_packet(session=session, body=MsgContainer([message])),
            )
    assert not session.results[request].event.is_set()
    assert session.recent_msg_ids == [old_id]
    assert old_id not in session.stored_msg_ids
    assert session.ignore_count == 0


@pytest.mark.parametrize("stored", [False, True])
async def test_fresh_item_resets_rejection_count_even_without_stored_history(
    stored: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _started_session()
    old_id = await session.msg_factory.allocate_message_identity() + 1
    fresh_id = await session.msg_factory.allocate_message_identity() + 1
    session.recent_msg_ids = [old_id]
    if stored:
        session.stored_msg_ids = [old_id - 4]
    session.ignore_count = 7
    times: list[int] = []
    monkeypatch.setattr(session.client, "_set_server_time", times.append)
    body = raw.types.NewSessionCreated(first_msg_id=1, unique_id=2, server_salt=3)
    await session.handle_packet(
        await _server_packet(
            session=session,
            body=MsgContainer(
                [
                    Message(body, old_id, 0, len(body)),
                    Message(body, fresh_id, 0, len(body)),
                ]
            ),
        )
    )
    assert session.ignore_count == 0
    assert fresh_id in session.stored_msg_ids
    assert times == [fresh_id]


@pytest.mark.parametrize("rejection", ["below-min", "too-old", "too-new"])
async def test_multiple_rejections_reach_threshold_before_a_fresh_sibling(
    rejection: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _started_session()
    session.ignore_count = Session.MAX_CONSECUTIVE_IGNORED - 2
    restarted: list[str] = []
    monkeypatch.setattr(session, "schedule_restart", restarted.append)
    request = await session.msg_factory.allocate_message_identity()
    session.results[request] = Result()
    body = raw.types.Pong(msg_id=request, ping_id=3)
    fresh_id = await session.msg_factory.allocate_message_identity() + 1
    old_id = fresh_id - 8
    session.stored_msg_ids = [old_id + 4]
    if rejection == "too-old":
        old_id -= 301 << 32
        session.stored_msg_ids = [old_id - 4]
    elif rejection == "too-new":
        old_id += 31 << 32
    times: list[int] = []
    monkeypatch.setattr(session.client, "_set_server_time", times.append)
    await session.handle_packet(
        await _server_packet(
            session=session,
            body=MsgContainer(
                [
                    Message(body, old_id, 1, len(body)),
                    Message(body, old_id, 1, len(body)),
                    Message(body, fresh_id, 2, len(body)),
                ]
            ),
        )
    )
    assert len(restarted) == 1
    assert session.ignore_count == Session.MAX_CONSECUTIVE_IGNORED
    assert not session.results[request].event.is_set()
    assert session.pending_acks == set()
    assert old_id not in session.stored_msg_ids
    assert times == []


@pytest.mark.parametrize("history", ["recent", "stored"])
async def test_known_duplicate_reacks_across_containers_without_dispatch_or_clock_change(
    history: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _started_session()
    monkeypatch.setattr(Session, "ACKS_THRESHOLD", 1)
    old_id = await session.msg_factory.allocate_message_identity() + 1
    getattr(session, f"{history}_msg_ids").append(old_id)
    session.ignore_count = 7
    times: list[int] = []
    monkeypatch.setattr(session.client, "_set_server_time", times.append)
    body = raw.types.UpdatesTooLong()
    seen: list[TLObject] = []

    async def handle_updates(body: TLObject) -> None:
        seen.append(body)

    monkeypatch.setattr(session.client, "handle_updates", handle_updates)
    for seq_no in (1, 1, 0):
        await session.handle_packet(
            await _server_packet(
                session=session,
                body=MsgContainer(
                    [
                        Message(body, old_id, seq_no, len(body)),
                    ]
                ),
            )
        )
    assert times == []
    await session._wait_pending_tasks()
    assert seen == []
    assert session.ignore_count == 7
    assert getattr(session, f"{history}_msg_ids") == [old_id]
    assert session.pending_acks == set()
    assert isinstance(session.connection, StubConnection)
    assert len(session.connection.sent) == 2
    for payload in session.connection.sent:
        ack = _client_message(session, payload).body
        assert isinstance(ack, raw.types.MsgsAck)
        assert ack.msg_ids == [old_id]


async def test_duplicate_only_packet_flushes_prior_and_repeated_acks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _started_session()
    monkeypatch.setattr(Session, "ACKS_THRESHOLD", 1)
    valid_id = await session.msg_factory.allocate_message_identity() + 1
    duplicate_id = await session.msg_factory.allocate_message_identity() + 1
    session.pending_acks = {valid_id}
    session.recent_msg_ids = [duplicate_id]
    body = raw.types.NewSessionCreated(first_msg_id=1, unique_id=2, server_salt=3)
    await session.handle_packet(
        await _server_packet(
            session=session,
            body=MsgContainer([Message(body, duplicate_id, 1, len(body))]),
        )
    )
    assert isinstance(session.connection, StubConnection)
    assert len(session.connection.sent) == 1
    payload = session.connection.sent[0]
    aes_key, aes_iv = mtproto.kdf(session.auth_key, payload[8:24], True)
    plain = BytesIO(aes.ige256_decrypt(payload[24:], aes_key, aes_iv))
    plain.read(16)
    ack = Message.read(plain).body
    assert isinstance(ack, raw.types.MsgsAck)
    assert set(ack.msg_ids) == {valid_id, duplicate_id}
    assert session.pending_acks == set()


async def test_valid_sibling_still_acks_and_dispatches_update_after_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _started_session()
    old_id = await session.msg_factory.allocate_message_identity() + 1
    fresh_id = await session.msg_factory.allocate_message_identity() + 1
    session.recent_msg_ids = [old_id]
    seen: list[TLObject] = []

    async def handle_updates(body: TLObject) -> None:
        seen.append(body)

    monkeypatch.setattr(session.client, "handle_updates", handle_updates)
    body = raw.types.UpdatesTooLong()
    container = MsgContainer(
        [
            Message(body, old_id, 1, len(body)),
            Message(body, fresh_id, 1, len(body)),
            Message(body, fresh_id, 1, len(body)),
        ]
    )
    for _ in range(2):
        await session.handle_packet(await _server_packet(session=session, body=container))
    await session._wait_pending_tasks()
    assert seen == [body]
    assert session.pending_acks == {old_id, fresh_id}
    assert session.ignore_count == 0


async def test_start_handshake_accepts_fresh_pong_after_replayed_container_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session()
    monkeypatch.setattr(Session, "ACKS_THRESHOLD", 1)
    old_ids = [await session.msg_factory.allocate_message_identity() + 1 for _ in range(2)]
    session.recent_msg_ids = old_ids.copy()

    class HandshakeConnection(StubConnection):
        def __init__(self) -> None:
            super().__init__()
            self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
            self.handshake_states: list[SessionState] = []

        async def connect(self) -> None:
            pass

        async def recv(self) -> bytes | None:
            return await self.incoming.get()

        async def close(self) -> None:
            await self.incoming.put(None)

        async def send(self, payload: bytes) -> None:
            self.sent.append(payload)
            request = _client_message(session, payload)
            if isinstance(request.body, raw.types.MsgsAck):
                return
            self.handshake_states.append(session.state)
            if isinstance(request.body, raw.functions.Ping):
                pong = raw.types.Pong(msg_id=request.msg_id, ping_id=request.body.ping_id)
                old_session = raw.types.NewSessionCreated(
                    first_msg_id=1, unique_id=2, server_salt=3
                )
                old_result = raw.types.RpcResult(
                    req_msg_id=4, result=raw.types.Pong(msg_id=4, ping_id=1)
                )
                bodies = [old_session, old_result, pong]
                ids = [*old_ids, await session.msg_factory.allocate_message_identity() + 1]
                reply: TLObject = MsgContainer(
                    [
                        Message(body, msg_id, seq_no, len(body))
                        for body, msg_id, seq_no in zip(bodies, ids, (1, 3, 4), strict=True)
                    ]
                )
            else:
                assert isinstance(request.body, raw.functions.InvokeWithLayer)
                assert isinstance(request.body.query, raw.functions.InitConnection)
                assert isinstance(request.body.query.query, raw.functions.help.GetConfig)
                config = raw.types.Config(
                    date=0,
                    expires=0,
                    test_mode=True,
                    this_dc=_DC_ID,
                    dc_options=[],
                    dc_txt_domain_name="",
                    chat_size_max=0,
                    megagroup_size_max=0,
                    forwarded_count_max=0,
                    online_update_period_ms=0,
                    offline_blur_timeout_ms=0,
                    offline_idle_timeout_ms=0,
                    online_cloud_timeout_ms=0,
                    notify_cloud_delay_ms=0,
                    notify_default_delay_ms=0,
                    push_chat_period_ms=0,
                    push_chat_limit=0,
                    edit_time_limit=0,
                    revoke_time_limit=0,
                    revoke_pm_time_limit=0,
                    rating_e_decay=0,
                    stickers_recent_limit=0,
                    channels_read_media_period=0,
                    call_receive_timeout_ms=0,
                    call_ring_timeout_ms=0,
                    call_connect_timeout_ms=0,
                    call_packet_timeout_ms=0,
                    me_url_prefix="",
                    caption_length_max=0,
                    message_length_max=0,
                    webfile_dc_id=_DC_ID,
                )
                reply = raw.types.RpcResult(req_msg_id=request.msg_id, result=config)
            await self.incoming.put(await _server_packet(session=session, body=reply))

    connection = HandshakeConnection()
    monkeypatch.setattr(session.client, "connection_factory", lambda **kwargs: connection)

    async def api_id() -> int:
        return 1

    monkeypatch.setattr(session.client.storage, "api_id", api_id)
    await asyncio.wait_for(session.start(), 1)
    try:
        assert session.state is SessionState.STARTED
        assert session.is_started.is_set()
        assert connection.handshake_states == [SessionState.STARTING] * 2
        acks = [_client_message(session, payload).body for payload in connection.sent]
        assert any(
            isinstance(ack, raw.types.MsgsAck) and set(ack.msg_ids) == set(old_ids) for ack in acks
        )
        assert session.recent_msg_ids == old_ids
        assert session.ignore_count == 0
    finally:
        await session.stop()

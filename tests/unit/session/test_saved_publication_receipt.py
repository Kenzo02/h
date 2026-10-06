import pytest

from pyrogram import raw
from pyrogram.session.session import PublicationUnconfirmed, _publication_receipt


def query(peer):
    return raw.functions.messages.SendMessage(peer=peer, random_id=71, message="Fixture")


@pytest.mark.parametrize("peer_kind", ["self", "own_user"])
@pytest.mark.parametrize("envelope", ["short", "updates"])
def test_saved_messages_native_receipt_does_not_require_out_flag(peer_kind, envelope):
    peer = (
        raw.types.InputPeerSelf()
        if peer_kind == "self"
        else raw.types.InputPeerUser(user_id=999, access_hash=1)
    )
    if envelope == "short":
        result = raw.types.UpdateShortSentMessage(id=902, pts=1, pts_count=1, date=1, out=False)
    else:
        result = raw.types.Updates(
            updates=[
                raw.types.UpdateMessageID(random_id=71, id=902),
                raw.types.UpdateNewMessage(
                    message=raw.types.Message(
                        id=902, peer_id=raw.types.PeerUser(user_id=999), date=1, message="Fixture", out=False
                    ),
                    pts=1,
                    pts_count=1,
                ),
            ],
            users=[],
            chats=[],
            date=1,
            seq=1,
        )
    assert _publication_receipt(query(peer), result, (71,), 999) == (902,)


@pytest.mark.parametrize("wrong_binding", ["peer", "query", "random_id", "duplicate"])
def test_incoming_saved_message_receipt_keeps_query_and_peer_guards(wrong_binding):
    peer = raw.types.InputPeerSelf()
    message = raw.types.Message(
        id=902, peer_id=raw.types.PeerUser(user_id=999), date=1, message="Fixture", out=False
    )
    mapping = raw.types.UpdateMessageID(random_id=71, id=902)
    if wrong_binding == "peer":
        message.peer_id = raw.types.PeerUser(user_id=1000)
    elif wrong_binding == "query":
        peer = raw.types.InputPeerUser(user_id=1000, access_hash=1)
        message.peer_id = raw.types.PeerUser(user_id=1000)
    elif wrong_binding == "random_id":
        mapping.random_id = 72
    updates = [mapping, raw.types.UpdateNewMessage(message=message, pts=1, pts_count=1)]
    if wrong_binding == "duplicate":
        updates.append(mapping)
    result = raw.types.Updates(updates=updates, users=[], chats=[], date=1, seq=1)
    with pytest.raises(PublicationUnconfirmed):
        _publication_receipt(query(peer), result, (71,), 999)

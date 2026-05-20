from io import BytesIO

from pyrogram import raw
from pyrogram.raw.core import TLObject


def _text(text=""):
    return raw.types.TextWithEntities(text=text, entities=[])


def test_optional_empty_vector_roundtrips_in_type():
    poll = raw.types.Poll(
        id=1,
        question=_text("Question"),
        answers=[raw.types.PollAnswer(text=_text("Answer"), option=b"0")],
        countries_iso2=[],
        hash=2,
    )

    parsed = TLObject.read(BytesIO(poll.write()))

    assert parsed.hash == 2
    assert parsed.countries_iso2 == []


def test_optional_empty_vector_roundtrips_in_function():
    request = raw.functions.bots.EditAccessSettings(
        bot=raw.types.InputUser(user_id=1, access_hash=2),
        add_users=[],
    )

    parsed = TLObject.read(BytesIO(request.write()))

    assert parsed.add_users == []

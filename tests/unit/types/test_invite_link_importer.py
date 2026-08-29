from datetime import datetime
from inspect import Parameter, iscoroutinefunction, signature

import pytest

from pyrogram import types
from pyrogram.types import ChatJoiner, InviteLinkImporter


def test_invite_link_importer_preserves_legacy_constructor():
    parameters = signature(InviteLinkImporter).parameters

    assert list(parameters) == ["date", "user"]
    assert all(parameter.kind is Parameter.KEYWORD_ONLY for parameter in parameters.values())

    date = datetime(2026, 8, 29, 12, 34, 56)
    user = types.User(id=123)
    importer = InviteLinkImporter(date=date, user=user)

    assert importer.date is date
    assert importer.user is user
    assert importer._client is None

    with pytest.raises(TypeError):
        InviteLinkImporter(date, user)


def test_invite_link_importer_does_not_define_a_second_parser():
    assert issubclass(InviteLinkImporter, ChatJoiner)
    assert "_parse" not in InviteLinkImporter.__dict__
    assert iscoroutinefunction(ChatJoiner._parse)

from types import SimpleNamespace

import pytest

from pyrogram import types
from pyrogram.methods.bots.create_invoice_link import CreateInvoiceLink


class Client(CreateInvoiceLink):
    test_mode = False

    async def invoke(self, query):
        self.query = query
        return SimpleNamespace(url="https://t.me/invoice")


@pytest.mark.asyncio
async def test_create_invoice_link_keeps_subscription_period_seconds():
    client = Client()

    await client.create_invoice_link(
        title="Monthly",
        description="Subscription",
        payload="payload",
        currency="XTR",
        prices=[types.LabeledPrice("Monthly", 1)],
        subscription_period=2592000,
    )

    invoice = client.query.invoice_media.invoice

    assert invoice.subscription_period == 2592000

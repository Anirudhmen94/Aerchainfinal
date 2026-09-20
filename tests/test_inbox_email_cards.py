"""Inbox email cards stay aligned with parsed quotes (Compare source of truth)."""
from __future__ import annotations

from shared_models import ExtractedQuote, InboxMessage, LineItem, QuestionnaireItem, RFx, Vendor
from orchestrator.pipeline import RFxPipeline, _body_from_quote, inbox_email_cards


def _rfx() -> RFx:
    return RFx(
        rfx_id="RFX-DEMO-1",
        title="Corrugated",
        scope="Shippers",
        terms="INR",
        line_items=[LineItem(line_id="L01", description="3-ply", qty=1000, uom="piece")],
        questionnaire=[QuestionnaireItem(id="Q1", question="ISO 9001?", knockout=True)],
        vendors=[Vendor(vendor_id="V01", name="PackForge", email="bid@packforge.example")],
    )


def test_body_from_quote_lists_prices_and_answers():
    q = ExtractedQuote(
        vendor_id="V01",
        source_format="xlsx",
        lines=[
            {
                "line_id": "L01",
                "description": "3-ply RSC",
                "unit_price": 1850.0,
                "uom": "per 100 pcs",
                "currency": "INR",
            }
        ],
        questionnaire_answers=[{"id": "Q1", "answer": "Yes"}],
        notes="Per 100 pcs.",
    )
    body = _body_from_quote(q, vendor_name="PackForge", rfx_id="RFX-DEMO-1")
    assert "Thanks for the RFx RFX-DEMO-1" in body
    assert "L01" in body
    assert "1,850.00" in body
    assert "INR" in body
    assert "per 100 pcs" in body
    assert "Q1: Yes" in body
    assert "Per 100 pcs." in body
    assert "PackForge" in body


def test_inbox_cards_use_quote_after_parse(tmp_path, monkeypatch):
    pipe = RFxPipeline()
    pipe.rfx = _rfx()
    # Point inbox dir at tmp so we don't touch real data
    monkeypatch.setattr(pipe, "_inbox_dir", lambda: tmp_path)

    bin_path = tmp_path / "V01_ignore_template.xlsx"
    bin_path.write_bytes(b"PK\x03\x04fake")

    pipe.inbox = [
        InboxMessage(
            msg_id="msg-1",
            vendor_id="V01",
            vendor_name="PackForge",
            subject="Re: RFx RFX-DEMO-1 — quotation",
            from_addr="bid@packforge.example",
            path=str(bin_path),
            body_preview="(file)",
            status="new",
        )
    ]

    # Before parse: attachment stub
    cards = inbox_email_cards(pipe)
    assert len(cards) == 1
    assert "Attachment: V01_ignore_template.xlsx" in cards[0]["body"]
    assert cards[0]["aligned"] is False
    assert cards[0]["from_email"] == "bid@packforge.example"

    # After parse: same quote object drives the body
    quote = ExtractedQuote(
        vendor_id="V01",
        source_format="xlsx",
        lines=[
            {
                "line_id": "L01",
                "description": "3-ply RSC",
                "unit_price": 1850.0,
                "uom": "per 100 pcs",
                "currency": "INR",
            }
        ],
        questionnaire_answers=[{"id": "Q1", "answer": "Yes"}],
        notes="Excel per 100.",
    )
    pipe.quotes = [quote]
    pipe.inbox[0].status = "parsed"

    cards2 = inbox_email_cards(pipe)
    assert cards2[0]["aligned"] is True
    assert cards2[0]["status"] == "parsed"
    assert "1,850.00" in cards2[0]["body"]
    assert "Q1: Yes" in cards2[0]["body"]
    # Must be the live quote — mutate and re-read
    quote.lines[0]["unit_price"] = 9999.0
    cards3 = inbox_email_cards(pipe)
    assert "9,999.00" in cards3[0]["body"]


def test_text_file_shown_before_parse(tmp_path, monkeypatch):
    pipe = RFxPipeline()
    pipe.rfx = _rfx()
    monkeypatch.setattr(pipe, "_inbox_dir", lambda: tmp_path)
    mail = tmp_path / "V05_oneline_email.txt"
    mail.write_text(
        "From: Nest <n@x>\nTo: procurement@buyer.example\nSubject: Re: RFX\n\n₹42/kg for the 5-ply.\n",
        encoding="utf-8",
    )
    pipe.inbox = [
        InboxMessage(
            msg_id="msg-5",
            vendor_id="V01",
            vendor_name="NestPack",
            subject="Re: RFX",
            from_addr="n@x",
            path=str(mail),
            status="new",
        )
    ]
    cards = inbox_email_cards(pipe)
    assert "₹42/kg for the 5-ply." in cards[0]["body"]
    assert cards[0]["aligned"] is False

"""Unit tests for agents.rfx_drafter — Anthropic client is mocked; no live API key."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import importlib.util
import sys
from pathlib import Path

import pytest

from shared_models import RFx, Vendor

_REPO = Path(__file__).resolve().parents[1]
_MOD_PATH = _REPO / "agents" / "rfx_drafter.py"
_spec = importlib.util.spec_from_file_location("agents.rfx_drafter", _MOD_PATH)
assert _spec and _spec.loader
# Ensure a lightweight package stub exists without executing agents/__init__.py
if "agents" not in sys.modules:
    import types
    _pkg = types.ModuleType("agents")
    _pkg.__path__ = [str(_REPO / "agents")]
    sys.modules["agents"] = _pkg
_mod = importlib.util.module_from_spec(_spec)
sys.modules["agents.rfx_drafter"] = _mod
_spec.loader.exec_module(_mod)
DEFAULT_HAIKU_MODEL = _mod.DEFAULT_HAIKU_MODEL
RFxDraftError = _mod.RFxDraftError
draft_rfx = _mod.draft_rfx
nominal_weight_g = _mod.nominal_weight_g


def _line(i: int) -> dict[str, Any]:
    ply = 3 if i % 2 else 5
    return {
        "line_id": f"L{i:02d}",
        "description": f"{ply}-ply RSC carton {300 + i}x{200 + i}x{150 + i} mm, B flute",
        "qty": 10000 + i * 100,
        "uom": "piece",
        "specs": {
            "length_mm": 300 + i,
            "width_mm": 200 + i,
            "height_mm": 150 + i,
            "ply": ply,
            "gsm": 450 if ply == 3 else 750,
            "flute": "B" if ply == 3 else "BC",
            "print": "1 colour flexo" if i % 3 == 0 else "Unprinted",
        },
    }


def _questions() -> list[dict[str, Any]]:
    qs = [
        ("Q1", "Can you supply FSC-certified board for all SKUs?", True),
        ("Q2", "Do you hold a valid ISO 9001 certificate?", True),
        ("Q3", "Can you meet BCT/ECT targets stated in the RFx?", True),
        ("Q4", "What is your standard lead time for a 30-SKU mix?", False),
        ("Q5", "Will you hold buffer stock for peak months?", False),
        ("Q6", "Share three references in FMCG / pharma packaging.", False),
        ("Q7", "What recycled content % can you guarantee?", False),
        ("Q8", "Do you offer moisture-barrier board options?", False),
        ("Q9", "Confirm plant capacity for 2M pieces/year.", False),
        ("Q10", "Are food-contact grades available on request?", False),
    ]
    return [{"id": i, "question": q, "knockout": ko} for i, q, ko in qs]


def _vendors(n: int = 5) -> list[dict[str, Any]]:
    names = [
        ("V1", "Sri Balaji Packaging Pvt Ltd", "quotes@sribalaji-packaging.in"),
        ("V2", "Kraftline Corrugators", "rfq@kraftline.co.in"),
        ("V3", "PakAsia Export Packaging", "sales@pakasia.in"),
        ("V4", "Meghna Boxes & Boards", "rates@meghnaboxes.in"),
        ("V5", "Ganesh Paper Products", "enquiry@ganeshpaper.in"),
    ]
    return [
        {"vendor_id": vid, "name": name, "email": email}
        for vid, name, email in names[:n]
    ]


def _payload(n_lines: int = 30, n_vendors: int = 5) -> dict[str, Any]:
    return {
        "title": "Annual corrugated packaging RFx — Pune plant",
        "scope": (
            "Buyer seeks annual supply of RSC and die-cut corrugated cartons "
            "for the Pune FMCG plant. Mix of 3-ply and 5-ply, delivered DAP."
        ),
        "terms": (
            "INR per piece, delivered to Pune plant, exclusive of GST. "
            "Payment 45 days from GRN. Quote validity 60 days. Freight included."
        ),
        "currency": "INR",
        "line_items": [_line(i) for i in range(1, n_lines + 1)],
        "questionnaire": _questions(),
        "vendors": _vendors(n_vendors),
    }


def _tool_response(payload: dict[str, Any]) -> SimpleNamespace:
    tool = SimpleNamespace(
        type="tool_use",
        id="toolu_test_1",
        name="emit_rfx",
        input=payload,
    )
    return SimpleNamespace(
        content=[tool],
        stop_reason="tool_use",
        model=DEFAULT_HAIKU_MODEL,
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
    )


def _mock_client(side_effect_payloads: list[dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    client.messages.create.side_effect = [_tool_response(p) for p in side_effect_payloads]
    return client


BRIEF = (
    "We need an annual RFx for corrugated boxes for our Pune plant: "
    "mix of 3-ply and 5-ply RSC, roughly 1.8M pieces/year across ~30 SKUs."
)


def test_draft_rfx_happy_path_exactly_30_lines_and_required_fields():
    client = _mock_client([_payload(30)])
    rfx = draft_rfx(BRIEF, client=client)

    assert isinstance(rfx, RFx)
    assert len(rfx.line_items) == 30
    assert rfx.title
    assert rfx.scope
    assert rfx.terms
    assert rfx.currency == "INR"
    assert rfx.rfx_id.startswith("RFX-")
    assert 8 <= len(rfx.questionnaire) <= 12
    assert sum(1 for q in rfx.questionnaire if q.knockout) >= 3
    assert len(rfx.vendors) == 5
    assert all(isinstance(v, Vendor) for v in rfx.vendors)
    assert all(li.description and li.qty > 0 for li in rfx.line_items)
    # optional weight enrichment
    assert "nominal_weight_g" in rfx.line_items[0].specs
    assert client.messages.create.call_count == 1
    call_kwargs = client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == DEFAULT_HAIKU_MODEL
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "emit_rfx"}


def test_draft_rfx_retries_when_first_response_has_wrong_line_count():
    client = _mock_client([_payload(22), _payload(30)])
    rfx = draft_rfx(BRIEF, client=client)

    assert len(rfx.line_items) == 30
    assert client.messages.create.call_count == 2
    second_user = client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert "22 line items" in second_user
    assert "exactly 30" in second_user.lower()


def test_draft_rfx_raises_after_retry_still_wrong_count():
    client = _mock_client([_payload(18), _payload(25)])
    with pytest.raises(RFxDraftError, match="exactly 30"):
        draft_rfx(BRIEF, client=client)
    assert client.messages.create.call_count == 2


def test_draft_rfx_seeds_vendors_when_model_returns_none():
    payload = _payload(30, n_vendors=0)
    payload["vendors"] = []
    client = _mock_client([payload])
    rfx = draft_rfx(BRIEF, client=client)
    assert len(rfx.vendors) == 5
    assert all(v.email and v.name for v in rfx.vendors)


def test_draft_rfx_rejects_short_brief():
    with pytest.raises(ValueError, match="too short"):
        draft_rfx("too short", client=MagicMock())


def test_draft_rfx_requires_knockout_questions():
    payload = _payload(30)
    for q in payload["questionnaire"]:
        q["knockout"] = False
    client = _mock_client([payload])
    with pytest.raises(RFxDraftError, match="knockout"):
        draft_rfx(BRIEF, client=client)


def test_nominal_weight_g_rsc_formula():
    # 400x300x250 mm, 750 gsm — sanity check positive and rounded to 1 dp
    w = nominal_weight_g(400, 300, 250, 750)
    assert w > 0
    assert isinstance(w, float)

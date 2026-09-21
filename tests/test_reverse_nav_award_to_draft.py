"""Regression: reverse navigation Award → … → Draft must not 500 or wipe lines.

Root causes previously observed:
- ensure_auto_awards → suggest_awards set wizard_step="award" while rendering Draft
- ensure_comparison ran on every step when quotes existed without comparison
- Jinja |tojson 500'd on non-JSON-serializable award/dispatch state (e.g. Path)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared_models import (
    ComparisonTable,
    ExtractedQuote,
    LineItem,
    NormalizedCell,
    QuestionnaireItem,
    RFx,
    Vendor,
    VendorQualification,
)
from orchestrator.pipeline import RFxPipeline


def _full_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RFxPipeline:
    for key in list(os.environ):
        if "BLOB" in key or key == "VERCEL":
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", tmp_path)
    import app_crew

    monkeypatch.setattr(app_crew, "STORE_DIR", tmp_path)
    app_crew._SESSIONS.clear()

    pipe = RFxPipeline()
    pipe.rfx = RFx(
        rfx_id="RFX-REVNAV-1",
        title="Reverse nav regression",
        scope="Corrugated cartons for Chakan plant",
        terms="INR per piece",
        line_items=[
            LineItem(line_id="L1", description="3-ply RSC 300x200x150", qty=1000, uom="ea"),
            LineItem(line_id="L2", description="5-ply RSC 400x300x200", qty=500, uom="ea"),
            LineItem(line_id="L3", description="Partition set", qty=200, uom="set"),
        ],
        questionnaire=[QuestionnaireItem(id="Q1", question="ISO 9001?", knockout=True)],
        vendors=[
            Vendor(vendor_id="v1", name="Alpha Pack", email="a@x.com"),
            Vendor(vendor_id="v2", name="Beta Board", email="b@x.com"),
            Vendor(vendor_id="v3", name="Gamma Corr", email="g@x.com"),
        ],
    )
    pipe.brief = (
        "Need corrugated SKUs for Chakan plant with ISO 9001 knockout. "
        "Happy-path brief for reverse-nav regression."
    )
    pipe.quotes = [
        ExtractedQuote(
            vendor_id="v1",
            source_format="json",
            lines=[
                {"line_id": "L1", "unit_price": 100},
                {"line_id": "L2", "unit_price": 50},
                {"line_id": "L3", "unit_price": 20},
            ],
            notes="ok",
        ),
        ExtractedQuote(
            vendor_id="v2",
            source_format="csv",
            lines=[
                {"line_id": "L1", "unit_price": 110},
                {"line_id": "L2", "unit_price": 40},
                {"line_id": "L3", "unit_price": 25},
            ],
            notes="ok",
        ),
        ExtractedQuote(
            vendor_id="v3",
            source_format="txt",
            lines=[
                {"line_id": "L1", "unit_price": 90},
                {"line_id": "L2", "unit_price": 35},
                {"line_id": "L3", "unit_price": 18},
            ],
            notes="ok",
        ),
    ]
    pipe.comparison = ComparisonTable(
        rfx_id=pipe.rfx.rfx_id,
        cells=[
            NormalizedCell(line_id="L1", vendor_id="v1", unit_price_inr=100.0, status="ok"),
            NormalizedCell(line_id="L1", vendor_id="v2", unit_price_inr=110.0, status="ok"),
            NormalizedCell(line_id="L1", vendor_id="v3", unit_price_inr=90.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v1", unit_price_inr=50.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v2", unit_price_inr=40.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v3", unit_price_inr=35.0, status="ok"),
            NormalizedCell(line_id="L3", vendor_id="v1", unit_price_inr=20.0, status="ok"),
            NormalizedCell(line_id="L3", vendor_id="v2", unit_price_inr=25.0, status="ok"),
            NormalizedCell(line_id="L3", vendor_id="v3", unit_price_inr=18.0, status="ok"),
        ],
        qualifications=[
            VendorQualification(vendor_id="v1", passed=True, award_eligible=True),
            VendorQualification(vendor_id="v2", passed=True, award_eligible=True),
            VendorQualification(
                vendor_id="v3",
                passed=False,
                award_eligible=False,
                reasons=["Failed KO Q1"],
            ),
        ],
        award_eligible_vendors=["v1", "v2"],
        qualified_vendors=["v1", "v2"],
        vendor_names={"v1": "Alpha Pack", "v2": "Beta Board", "v3": "Gamma Corr"},
    )
    pipe.ensure_auto_awards()
    pipe.wizard_step = "award"
    pipe.chat = [{"question": "split?", "answer": "cheapest pass", "data": None}]
    # Non-serializable Path must not 500 Jinja |tojson after award actions
    pipe.dispatch_log.append(
        {
            "kind": "award_notice",
            "vendor_id": "v1",
            "path": Path(tmp_path / "award_v1.txt"),
        }
    )
    return pipe


@pytest.fixture()
def client_and_pipe(tmp_path, monkeypatch):
    import app_crew

    pipe = _full_pipe(tmp_path, monkeypatch)
    app_crew._SESSIONS[pipe.rfx.rfx_id] = pipe
    app_crew._save(pipe)
    client = TestClient(app_crew.app, raise_server_exceptions=False)
    return client, pipe, app_crew


def _assert_ok(client, rid: str, step: str, *, expect_lines: bool = True) -> None:
    r = client.get(f"/crew/{rid}/wizard?step={step}")
    html = r.text or ""
    assert r.status_code == 200, f"{step}: status={r.status_code} body={html[:400]}"
    for bad in (
        "Session missing",
        "FUNCTION_INVOCATION_FAILED",
        "Internal Server Error",
        "Something went wrong",
        "PosixPath",
        "not JSON serializable",
    ):
        assert bad not in html, f"{step}: symptom {bad!r}"
    if expect_lines and step == "draft":
        assert "3-ply RSC" in html or "L1" in html
        assert "Chakan" in html or "corrugated" in html.lower()
        assert "line items stay empty until you generate" not in html.lower() or "L1" in html


def test_forward_then_full_reverse(client_and_pipe):
    client, pipe, app_crew = client_and_pipe
    rid = pipe.rfx.rfx_id

    for step in ["draft", "send", "inbox", "compare", "ask", "award"]:
        _assert_ok(client, rid, step)

    for step in ["award", "ask", "compare", "inbox", "send", "draft"]:
        _assert_ok(client, rid, step)

    # Visiting draft must leave wizard_step on draft (not yanked back to award)
    assert app_crew._SESSIONS[rid].wizard_step == "draft"
    assert len(app_crew._SESSIONS[rid].rfx.line_items) == 3
    assert app_crew._SESSIONS[rid].comparison is not None
    assert app_crew._SESSIONS[rid].awards  # awards preserved


def test_award_compare_draft_skip(client_and_pipe):
    client, pipe, app_crew = client_and_pipe
    rid = pipe.rfx.rfx_id
    _assert_ok(client, rid, "award")
    _assert_ok(client, rid, "compare")
    _assert_ok(client, rid, "draft")
    assert app_crew._SESSIONS[rid].wizard_step == "draft"
    assert len(app_crew._SESSIONS[rid].rfx.line_items) == 3


def test_after_suggest_and_save(client_and_pipe):
    client, pipe, app_crew = client_and_pipe
    rid = pipe.rfx.rfx_id
    pipe.suggest_awards()
    pipe.wizard_step = "award"
    app_crew._save(pipe)
    _assert_ok(client, rid, "award")
    _assert_ok(client, rid, "draft")
    pipe.save_awards(dict(pipe.awards))
    app_crew._save(pipe)
    _assert_ok(client, rid, "award")
    _assert_ok(client, rid, "draft")
    assert len(app_crew._SESSIONS[rid].rfx.line_items) == 3


def test_ensure_auto_awards_preserves_wizard_step(tmp_path, monkeypatch):
    pipe = _full_pipe(tmp_path, monkeypatch)
    pipe.awards = {}
    pipe.suggested_awards = {}
    pipe.wizard_step = "draft"
    assert pipe.ensure_auto_awards() is True
    assert pipe.wizard_step == "draft"
    assert pipe.awards

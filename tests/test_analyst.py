"""Unit tests for AnalystAgent — no live Anthropic API calls."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared_models import ComparisonTable, LineItem, NormalizedCell, QuestionnaireItem, RFx, Vendor
from agents.analyst import (
    AnalystAgent,
    answer,
    ask_question,
    build_comparison_markdown,
    compute_cheapest_per_qualified,
    compute_vendor_totals,
    normalize_comparison_state,
    shortlist_vendors,
    suggest_split_award,
    validate_award,
    _safe_calculate,
)


def _sample_cells():
    return [
        NormalizedCell(line_id="L1", vendor_id="v1", unit_price_inr=10.0, status="ok"),
        NormalizedCell(line_id="L1", vendor_id="v2", unit_price_inr=8.0, status="ok"),
        NormalizedCell(line_id="L1", vendor_id="v3", unit_price_inr=7.0, status="ok"),  # not qualified
        NormalizedCell(line_id="L2", vendor_id="v1", unit_price_inr=20.0, status="converted"),
        NormalizedCell(
            line_id="L2",
            vendor_id="v2",
            unit_price_inr=22.0,
            status="converted",
            original_currency="USD",
            original_price=0.26,
            flags=["usd_fx"],
        ),
        NormalizedCell(line_id="L2", vendor_id="v3", unit_price_inr=5.0, status="missing"),  # unusable
        NormalizedCell(line_id="L3", vendor_id="v1", unit_price_inr=None, status="missing"),
        NormalizedCell(line_id="L3", vendor_id="v2", unit_price_inr=15.0, status="uncertain"),
    ]


def _sample_rfx() -> RFx:
    return RFx(
        rfx_id="rfx-demo",
        title="Corrugated",
        scope="packaging",
        terms="INR delivered",
        line_items=[
            LineItem(line_id="L1", description="Box A", qty=100, uom="piece"),
            LineItem(line_id="L2", description="Box B", qty=50, uom="piece"),
            LineItem(line_id="L3", description="Box C", qty=10, uom="piece"),
        ],
        questionnaire=[QuestionnaireItem(id="q1", question="ISO?", knockout=True)],
        vendors=[
            Vendor(vendor_id="v1", name="Alpha", email="a@x.com"),
            Vendor(vendor_id="v2", name="Beta", email="b@x.com"),
            Vendor(vendor_id="v3", name="Gamma", email="c@x.com"),
        ],
    )


def _sample_comparison_dict():
    return {
        "rfx_id": "rfx-demo",
        "cells": [c.model_dump() for c in _sample_cells()],
        "qualified_vendors": ["v1", "v2"],
        "vendor_flags": {"v2": ["freight_extra"], "v3": ["questionnaire_incomplete"]},
        "usd_rate": 83.50,
    }


def test_compute_cheapest_per_qualified_picks_min_among_qualified():
    cells = [c.model_dump() for c in _sample_cells()]
    rows = compute_cheapest_per_qualified(
        cells,
        qualified_vendors=["v1", "v2"],
        vendor_names={"v1": "Alpha", "v2": "Beta", "v3": "Gamma"},
        line_meta={"L1": {"qty": 100}, "L2": {"qty": 50}},
    )
    by_line = {r["line_id"]: r for r in rows}
    # L1: v2 at 8 beats v1 at 10; v3 at 7 excluded
    assert by_line["L1"]["vendor_id"] == "v2"
    assert by_line["L1"]["unit_price_inr"] == 8.0
    assert by_line["L1"]["vendor_name"] == "Beta"
    assert by_line["L1"]["extended_inr"] == 800.0
    # L2: v1 at 20 beats v2 at 22
    assert by_line["L2"]["vendor_id"] == "v1"
    assert by_line["L2"]["extended_inr"] == 1000.0
    # L3 has no usable qualified price
    assert "L3" not in by_line


def test_compute_cheapest_excludes_non_usable_status():
    cells = [
        {"line_id": "L1", "vendor_id": "v1", "unit_price_inr": 1.0, "status": "uncertain"},
        {"line_id": "L1", "vendor_id": "v2", "unit_price_inr": 9.0, "status": "ok"},
    ]
    rows = compute_cheapest_per_qualified(cells, qualified_vendors=["v1", "v2"])
    assert len(rows) == 1
    assert rows[0]["vendor_id"] == "v2"


def test_build_comparison_markdown_uses_emdash_for_missing():
    cells = [c.model_dump() for c in _sample_cells()]
    md = build_comparison_markdown(cells, {"v1": "Alpha", "v2": "Beta", "v3": "Gamma"})
    assert "| line_id |" in md or md.startswith("| line_id")
    assert "Alpha" in md and "Beta" in md
    assert "10.00" in md
    assert "—" in md  # missing/unusable cells


def test_vendor_totals_respect_qty_and_qualification():
    cells = [c.model_dump() for c in _sample_cells()]
    totals = compute_vendor_totals(
        cells,
        line_meta={"L1": {"qty": 100}, "L2": {"qty": 50}},
        qualified_vendors=["v1", "v2"],
        vendor_names={"v1": "Alpha", "v2": "Beta"},
    )
    by_id = {t["vendor_id"]: t for t in totals}
    # v1: 10*100 + 20*50 = 2000
    assert by_id["v1"]["approx_total_inr"] == 2000.0
    # v2: 8*100 + 22*50 = 1900
    assert by_id["v2"]["approx_total_inr"] == 1900.0
    assert "v3" not in by_id


def test_normalize_merges_rfx_enrichment():
    table = ComparisonTable(rfx_id="rfx-demo", cells=_sample_cells(), vendor_flags={})
    st = normalize_comparison_state(
        {**table.model_dump(), "qualified_vendors": ["v1", "v2"]},
        rfx=_sample_rfx(),
    )
    assert st["vendor_names"]["v1"] == "Alpha"
    assert st["line_meta"]["L1"]["qty"] == 100
    assert st["qualified_vendors"] == ["v1", "v2"]


def test_analyst_agent_deterministic_methods():
    agent = AnalystAgent(client=MagicMock())
    agent.load_comparison(_sample_comparison_dict(), rfx=_sample_rfx())
    rows = agent.cheapest_per_qualified()
    assert rows[0]["line_id"] == "L1"
    md = agent.comparison_markdown_table()
    assert "—" in md
    agent.chat_history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    agent.reset()
    assert agent.chat_history == []


def test_ask_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = AnalystAgent(client=None)
    agent.load_comparison(_sample_comparison_dict())
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        agent.ask("Who is cheapest?")


def test_ask_calls_claude_with_injected_data():
    mock_client = MagicMock()
    mock_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Beta wins L1 at 8 INR from data.")]
    )
    mock_client.messages.create.return_value = mock_resp

    agent = AnalystAgent(client=mock_client)
    agent.load_comparison(_sample_comparison_dict(), rfx=_sample_rfx())
    out = agent.ask("Who is cheapest on L1 among qualified vendors?")

    assert "Beta wins L1" in out
    assert mock_client.messages.create.called
    kwargs = mock_client.messages.create.call_args.kwargs
    assert "claude-3-5-sonnet" in kwargs["model"] or kwargs["model"]
    system = kwargs["system"]
    assert "COMPARISON_DATA" in system
    assert "never invent" in system.lower() or "NEVER invent" in system
    # history accumulates
    assert len(agent.chat_history) == 2
    assert agent.chat_history[0]["role"] == "user"
    assert agent.chat_history[1]["role"] == "assistant"


def test_get_award_recommendation_prefers_precomputed(monkeypatch):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Recommend split award.")]
    )
    agent = AnalystAgent(client=mock_client)
    agent.load_comparison(_sample_comparison_dict(), rfx=_sample_rfx())
    text = agent.get_award_recommendation()
    assert "Recommend split" in text
    user_content = mock_client.messages.create.call_args.kwargs["messages"][-1]["content"]
    assert "PRECOMPUTED_CHEAPEST_QUALIFIED" in user_content
    assert '"vendor_id": "v2"' in user_content or "v2" in user_content


def test_answer_offline_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = answer(
        "What if we split cheapest per line among qualified vendors?",
        rfx=_sample_rfx(),
        comparison=_sample_comparison_dict(),
    )
    assert "answer" in result
    assert "markdown" in result
    assert "tables" in result
    assert result["tables"]["cheapest_per_qualified"][0]["vendor_id"] == "v2"
    assert result["tool"] == "deterministic_offline"
    # No hardcoded demo narrative — numbers come from data
    assert "8.00" in result["answer"] or "8.0" in result["answer"] or "₹8" in result["answer"]


def test_answer_with_mocked_client_via_agent_path(monkeypatch):
    """Ensure answer() return shape when LLM works (patch AnalystAgent.ask)."""
    monkeypatch.setattr(
        AnalystAgent,
        "ask",
        lambda self, q: "Prose from Claude using injected tables.",
    )
    monkeypatch.setattr(
        AnalystAgent,
        "get_award_recommendation",
        lambda self: "Award memo from Claude.",
    )
    # Provide a dummy client so _ensure_client is not needed if ask is patched
    result = answer(
        "Give me an award recommendation for the VP",
        rfx=_sample_rfx(),
        comparison=_sample_comparison_dict(),
    )
    assert result["answer"] == "Award memo from Claude."
    assert result["tool"] == "award_recommendation"
    assert isinstance(result["tables"]["cheapest_per_qualified"], list)


def test_ask_question_manager_signature(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = ask_question(
        "Show vendor totals",
        _sample_rfx(),
        _sample_comparison_dict(),
    )
    assert result["tables"]["vendor_totals"]


def test_ask_question_legacy_table_first(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    table = ComparisonTable(
        rfx_id="rfx-demo",
        cells=_sample_cells(),
        vendor_flags={},
    )
    # Enrich via dict wrap for qualified list
    comparison = {**table.model_dump(), "qualified_vendors": ["v1", "v2"]}
    result = ask_question(comparison, "cheapest per line", _sample_rfx())
    assert result["tables"]["cheapest_per_qualified"]


def test_safe_calculate():
    assert _safe_calculate("10 + 5") == 15.0
    assert abs(_safe_calculate("percent(5, 200)") - 2.5) < 1e-9
    with pytest.raises(ValueError):
        _safe_calculate("__import__('os').system('x')")


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_SONNET_MODEL", "claude-test-model")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")]
    )
    agent = AnalystAgent(client=mock_client)
    agent.load_comparison(_sample_comparison_dict())
    agent.ask("ping")
    assert mock_client.messages.create.call_args.kwargs["model"] == "claude-test-model"

def test_answer_history_seeding_offline(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prior = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi — how can I help with the comparison?"},
        {"role": "user", "content": "", "extra": "ignore"},  # dropped empty
        {"role": "system", "content": "nope"},  # unknown role dropped
    ]
    result = answer(
        "Who is cheapest on L1 among qualified?",
        rfx=_sample_rfx(),
        comparison=_sample_comparison_dict(),
        history=prior,
    )
    assert "history" in result
    hist = result["history"]
    assert len(hist) == 4  # 2 prior + this turn pair
    assert hist[0] == {"role": "user", "content": "Hello"}
    assert hist[1]["role"] == "assistant"
    assert hist[2]["role"] == "user"
    assert "cheapest" in hist[2]["content"].lower() or "L1" in hist[2]["content"]
    assert hist[3]["role"] == "assistant"
    assert result["tool"] == "deterministic_offline"
    assert isinstance(hist[3]["content"], str) and hist[3]["content"]


def test_ask_question_forwards_history(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = ask_question(
        "totals please",
        _sample_rfx(),
        _sample_comparison_dict(),
        history=[{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}],
    )
    assert len(result["history"]) == 4


def test_validate_award_rejects_non_qualified_vendor():
    comparison = _sample_comparison_dict()
    result = validate_award(
        comparison,
        qualifications=["v1", "v2"],
        awards={"L1": "v3"},  # v3 not qualified
        rfx=_sample_rfx(),
    )
    assert result["ok"] is False
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["reason"] == "not_qualified"
    assert result["rejected"][0]["vendor_id"] == "v3"
    assert result["awards"] == []


def test_validate_award_rejects_vendor_without_usable_price():
    comparison = _sample_comparison_dict()
    # L3: v1 missing price, v2 uncertain — neither usable
    result = validate_award(
        comparison,
        qualifications=["v1", "v2"],
        awards={"L3": "v1"},
        rfx=_sample_rfx(),
    )
    assert result["ok"] is False
    assert result["rejected"][0]["reason"] == "no_usable_price"
    assert result["rejected"][0]["line_id"] == "L3"


def test_validate_award_computes_extended_totals_with_qty():
    comparison = _sample_comparison_dict()
    result = validate_award(
        comparison,
        qualifications=["v1", "v2"],
        awards={"L1": "v2", "L2": {"vendor_id": "v1"}},  # dict form accepted
        rfx=_sample_rfx(),
    )
    assert result["ok"] is True
    by_line = {a["line_id"]: a for a in result["awards"]}
    assert by_line["L1"]["vendor_id"] == "v2"
    assert by_line["L1"]["unit_price_inr"] == 8.0
    assert by_line["L1"]["qty"] == 100.0
    assert by_line["L1"]["extended_inr"] == 800.0
    assert by_line["L2"]["vendor_id"] == "v1"
    assert by_line["L2"]["extended_inr"] == 1000.0  # 20 * 50
    assert result["totals"]["grand_total_inr"] == 1800.0
    assert result["totals"]["grand_total_partial_inr"] == 1800.0
    assert result["totals"]["lines_awarded"] == 2
    assert result["totals"]["lines_rejected"] == 0
    assert "L3" in result["unawarded_lines"]
    assert "v2" in result["totals"]["by_vendor"]
    assert result["totals"]["by_vendor"]["v2"]["extended_inr"] == 800.0
    assert "line_id" in result["markdown"]


def test_suggest_split_award_matches_cheapest_qualified():
    comparison = _sample_comparison_dict()
    suggested = suggest_split_award(
        comparison,
        qualifications=["v1", "v2"],
        rfx=_sample_rfx(),
    )
    direct = validate_award(
        comparison,
        qualifications=["v1", "v2"],
        awards=None,
        rfx=_sample_rfx(),
        fill_missing_with_cheapest=True,
    )
    assert suggested["ok"] is True
    assert suggested["awards"] == direct["awards"]
    by_line = {a["line_id"]: a for a in suggested["awards"]}
    # L1 cheapest qualified = v2 @ 8; L2 = v1 @ 20
    assert by_line["L1"]["vendor_id"] == "v2"
    assert by_line["L2"]["vendor_id"] == "v1"
    assert "L3" not in by_line  # no usable qualified price
    assert by_line["L1"]["extended_inr"] == 800.0
    assert by_line["L2"]["extended_inr"] == 1000.0



def test_shortlist_vendors_coverage_and_pass():
    cmp = _sample_comparison_dict()
    rfx = _sample_rfx()
    rows = shortlist_vendors(cmp, rfx=rfx)
    by_id = {r["vendor_id"]: r for r in rows}
    assert set(by_id) >= {"v1", "v2", "v3"}
    assert by_id["v1"]["pass"] is True
    assert by_id["v2"]["pass"] is True
    assert by_id["v3"]["pass"] is False
    assert by_id["v1"]["name"] == "Alpha"
    # v1 usable on L1+L2 of 3 lines → 2/3
    assert by_id["v1"]["coverage"] == round(2 / 3, 4)
    # Qualified vendors should sort before failed ones
    assert rows[0]["pass"] is True
    assert rows[-1]["vendor_id"] == "v3" or rows[-1]["pass"] is False

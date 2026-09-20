"""Freeze award + append-only review log (enhancement #4)."""
from __future__ import annotations

from shared_models import (
    ComparisonTable,
    LineItem,
    NormalizedCell,
    QuestionnaireItem,
    RFx,
    Vendor,
    VendorQualification,
)
from orchestrator.pipeline import RFxPipeline


def _pipe_with_awards() -> RFxPipeline:
    pipe = RFxPipeline()
    pipe.rfx = RFx(
        rfx_id="RFX-TEST-FREEZE",
        title="Freeze test",
        scope="test",
        terms="INR",
        line_items=[
            LineItem(line_id="L1", description="Widget", qty=10, uom="piece"),
            LineItem(line_id="L2", description="Gadget", qty=5, uom="piece"),
        ],
        questionnaire=[QuestionnaireItem(id="Q1", question="ISO?", knockout=True)],
        vendors=[
            Vendor(vendor_id="v1", name="Alpha", email="a@x.com"),
            Vendor(vendor_id="v2", name="Beta", email="b@x.com"),
        ],
    )
    pipe.comparison = ComparisonTable(
        rfx_id=pipe.rfx.rfx_id,
        cells=[
            NormalizedCell(line_id="L1", vendor_id="v1", unit_price_inr=100.0, status="ok"),
            NormalizedCell(line_id="L1", vendor_id="v2", unit_price_inr=110.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v1", unit_price_inr=50.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v2", unit_price_inr=40.0, status="ok"),
        ],
        qualifications=[
            VendorQualification(vendor_id="v1", passed=True, award_eligible=True),
            VendorQualification(vendor_id="v2", passed=True, award_eligible=True),
        ],
        award_eligible_vendors=["v1", "v2"],
        qualified_vendors=["v1", "v2"],
        vendor_names={"v1": "Alpha", "v2": "Beta"},
    )
    pipe.save_awards({"L1": "v1", "L2": "v2"})
    return pipe


def test_save_logs_award_saved():
    pipe = _pipe_with_awards()
    actions = [e["action"] for e in pipe.review_log]
    assert "award_saved" in actions
    assert pipe.review_log[-1]["actor"] == "buyer"


def test_freeze_and_block_edits(tmp_path, monkeypatch):
    from orchestrator import pipeline as pl

    monkeypatch.setattr(pl, "STORE_DIR", tmp_path)
    pipe = _pipe_with_awards()
    snap = pipe.freeze_award(note="VP ready")
    assert snap["freeze_id"].startswith("FZ-")
    assert pipe.is_frozen
    assert snap["awards"] == {"L1": "v1", "L2": "v2"}
    assert "freeze" in [e["action"] for e in pipe.review_log]

    try:
        pipe.save_awards({"L1": "v2", "L2": "v2"})
        assert False, "expected freeze block"
    except RuntimeError as exc:
        assert "frozen" in str(exc).lower()

    pipe.unfreeze_award(note="reopen")
    assert not pipe.is_frozen
    assert "unfreeze" in [e["action"] for e in pipe.review_log]
    # Changing away from auto-suggested vendor queues a provisional override
    result = pipe.save_awards({"L1": "v2", "L2": "v2"})
    assert "L1" not in pipe.awards  # provisional until manager approval
    assert any(r.get("status") == "pending" and r.get("line_id") == "L1" for r in pipe.partial_requests)
    assert result.get("_overrides_pending")


def test_snapshot_roundtrip(tmp_path, monkeypatch):
    from orchestrator import pipeline as pl

    monkeypatch.setattr(pl, "STORE_DIR", tmp_path)
    pipe = _pipe_with_awards()
    pipe.freeze_award()
    pipe.log_cell_override("L1", "v1", "accepted converted price")
    data = pipe.snapshot()
    assert data["freeze"]["freeze_id"]
    assert any(e["action"] == "cell_override" for e in data["review_log"])

    other = RFxPipeline()
    other.load_snapshot(data)
    assert other.is_frozen
    assert other.freeze["freeze_id"] == pipe.freeze["freeze_id"]
    assert len(other.review_log) == len(pipe.review_log)

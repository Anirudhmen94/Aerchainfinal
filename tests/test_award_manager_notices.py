"""Award auto-suggest, override provisionals, firm-only notices, manager outbox stubs."""
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


def _pipe(tmp_path, monkeypatch) -> RFxPipeline:
    from orchestrator import pipeline as pl

    monkeypatch.setattr(pl, "STORE_DIR", tmp_path)
    monkeypatch.setattr(pl, "OUTBOX_DIR", tmp_path / "outbox")
    (tmp_path / "outbox").mkdir(parents=True, exist_ok=True)

    pipe = RFxPipeline()
    pipe.rfx = RFx(
        rfx_id="RFX-TEST-MGR",
        title="Manager notice test",
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
            Vendor(vendor_id="v3", name="Gamma", email="g@x.com"),
        ],
    )
    pipe.comparison = ComparisonTable(
        rfx_id=pipe.rfx.rfx_id,
        cells=[
            NormalizedCell(line_id="L1", vendor_id="v1", unit_price_inr=100.0, status="ok"),
            NormalizedCell(line_id="L1", vendor_id="v2", unit_price_inr=110.0, status="ok"),
            NormalizedCell(line_id="L1", vendor_id="v3", unit_price_inr=90.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v1", unit_price_inr=50.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v2", unit_price_inr=40.0, status="ok"),
            NormalizedCell(line_id="L2", vendor_id="v3", unit_price_inr=35.0, status="ok"),
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
        vendor_names={"v1": "Alpha", "v2": "Beta", "v3": "Gamma"},
    )
    return pipe


def test_ensure_auto_awards_fills_cheapest_pass(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch)
    assert pipe.awards == {}
    assert pipe.ensure_auto_awards() is True
    assert pipe.awards["L1"] == "v1"  # cheapest Pass (v3 cheaper but Fail)
    assert pipe.awards["L2"] == "v2"
    assert pipe.suggested_awards == pipe.awards
    assert pipe.ensure_auto_awards() is False  # already filled


def test_override_queues_manager_notice_and_stays_provisional(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch)
    pipe.ensure_auto_awards()
    result = pipe.save_awards({"L1": "v2", "L2": "v2"})
    assert "L1" not in pipe.awards
    assert pipe.awards.get("L2") == "v2"
    pending = [r for r in pipe.partial_requests if r["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "override"
    assert pending[0]["vendor_id"] == "v2"
    assert result["_overrides_pending"]
    mgr_rows = [r for r in pipe.dispatch_log if r.get("kind") == "manager"]
    assert mgr_rows
    assert "approval" in (mgr_rows[-1].get("notice_kind") or "")
    assert any("manager_approval" in str(p) or "manager_approval" in str(r.get("path") or "") or "manager_" in str(r.get("path") or "") for r, p in [(mgr_rows[-1], mgr_rows[-1].get("path"))] )


def test_partial_request_writes_manager_stub(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch)
    pipe.ensure_auto_awards()
    entry = pipe.request_partial_award("L1", "v3", "Need Gamma despite KO for demo continuity.")
    assert entry["status"] == "pending"
    assert entry["kind"] == "partial"
    assert any(r.get("kind") == "manager" for r in pipe.dispatch_log)
    # Freeze blocked while pending
    try:
        pipe.freeze_award()
        assert False, "expected freeze block"
    except RuntimeError as exc:
        assert "pending" in str(exc).lower()


def test_notify_firm_only_and_manager_summary(tmp_path, monkeypatch):
    pipe = _pipe(tmp_path, monkeypatch)
    pipe.ensure_auto_awards()
    # Override L1 → provisional; L2 remains firm
    pipe.save_awards({"L1": "v2", "L2": "v2"})
    paths = pipe.notify_awarded_vendors()
    assert paths
    # Only Beta (v2) should get an award notice (firm L2); L1 pending excluded
    vendors = {r.get("vendor_id") for r in pipe.award_log}
    assert vendors == {"v2"}
    mgr = [r for r in pipe.dispatch_log if r.get("kind") == "manager" and r.get("notice_kind") == "awards_sent"]
    assert mgr
    assert "Beta" in (mgr[-1].get("subject") or "") or "vendor" in (mgr[-1].get("subject") or "").lower()

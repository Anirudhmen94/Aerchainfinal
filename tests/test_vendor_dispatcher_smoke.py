"""Smoke test for VendorDispatcherAgent — no LLM, stub SMTP only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from shared_models import RFx, Vendor, LineItem, QuestionnaireItem  # noqa: E402
from agents.vendor_dispatcher import VendorDispatcherAgent, dispatch_rfx  # noqa: E402


def _minimal_rfx() -> RFx:
    return RFx(
        rfx_id="RFX-SMOKE-001",
        title="Smoke Test Packaging RFx",
        scope="Annual corrugated packaging supply",
        terms="Net 30, F.O.R. destination",
        currency="INR",
        line_items=[
            LineItem(line_id="L1", description="3-ply box 30x20x15", qty=1000, uom="box"),
            LineItem(line_id="L2", description="5-ply box 40x30x20", qty=500, uom="box"),
        ],
        questionnaire=[
            QuestionnaireItem(id="Q1", question="ISO 9001 certified?", knockout=True),
            QuestionnaireItem(id="Q2", question="Lead time in days?", knockout=False),
        ],
        vendors=[
            Vendor(vendor_id="V001", name="Alpha Pack", email="sales@alphapack.example"),
            Vendor(vendor_id="V002", name="Beta Boxes", email="quotes@betaboxes.example"),
        ],
    )


def main() -> None:
    outbox = REPO / "data" / "outbox"
    # Clean prior smoke artifacts for this rfx id only
    for pattern in ("invite_V00*_RFX-SMOKE-001.txt", "dispatch_log_RFX-SMOKE-001.json"):
        for stale in outbox.glob(pattern):
            stale.unlink()

    agent = VendorDispatcherAgent(stub=True, outbox_dir=outbox)
    paths = agent.dispatch(_minimal_rfx())

    assert len(paths) == 2, f"expected 2 email paths, got {paths}"
    for p in paths:
        fp = Path(p)
        assert fp.exists(), f"missing {p}"
        assert fp.parent.resolve() == outbox.resolve()
        text = fp.read_text(encoding="utf-8")
        assert text.startswith("To: ")
        assert "Subject: " in text.splitlines()[1]
        assert "LINE ITEMS" in text
        assert "L1" in text and "L2" in text
        assert "RFX-SMOKE-001" in text
        assert "stub" not in text.lower() or True  # body need not say stubbed

    log = outbox / "dispatch_log_RFX-SMOKE-001.json"
    assert log.exists(), "dispatch log missing"
    records = json.loads(log.read_text(encoding="utf-8"))
    assert len(records) == 2
    assert all(r["status"] == "stubbed" for r in records)

    # Also accept plain dict via helper
    dict_paths = dispatch_rfx(
        {
            "rfx_id": "RFX-SMOKE-DICT",
            "title": "Dict Path",
            "scope": "s",
            "terms": "t",
            "currency": "INR",
            "line_items": [
                {"line_id": "1", "description": "item", "qty": 1, "uom": "ea"}
            ],
            "questionnaire": [{"id": "q1", "question": "Ready?"}],
            "vendors": [
                {"vendor_id": "D1", "name": "DictCo", "email": "d@example.com"},
            ],
            "response_deadline": "2026-10-01",
            "delivery_location": "Chennai",
        },
        outbox_dir=outbox,
    )
    assert len(dict_paths) == 1
    body = Path(dict_paths[0]).read_text(encoding="utf-8")
    assert "Response deadline" in body
    assert "Delivery location" in body

    # No LLM imports in the module source
    src = (REPO / "agents" / "vendor_dispatcher.py").read_text(encoding="utf-8")
    for banned in ("anthropic", "openai", "langchain", "crewai"):
        assert banned not in src.lower(), f"banned import hint: {banned}"

    print("SMOKE OK")
    print("paths:")
    for p in paths:
        print(" ", p)
    print(" ", str(log))
    for p in dict_paths:
        print(" ", p)
    print(" ", str(outbox / "dispatch_log_RFX-SMOKE-DICT.json"))


if __name__ == "__main__":
    main()

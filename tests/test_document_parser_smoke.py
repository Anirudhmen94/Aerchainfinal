"""Smoke test for Document Parser agent (deterministic + Haiku)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.document_parser import parse_response, parse_vendor_file

SAMPLES = ROOT / "data" / "vendor_responses"
OUT = ROOT / "data" / "rfx" / "parser_smoke.json"

# Minimal 30-line corrugated RFx context for Haiku matching
RFX = {
    "rfx_id": "RFX-CORR-2026-001",
    "currency": "INR",
    "line_items": [
        {"line_id": f"L{i:02d}", "description": desc, "uom": uom, "qty": qty}
        for i, (desc, uom, qty) in enumerate(
            [
                ("3-ply RSC carton 300x200x150 mm", "ea", 12000),
                ("3-ply RSC carton 400x300x200 mm", "ea", 9000),
                ("3-ply RSC carton 500x400x300 mm", "ea", 6000),
                ("5-ply RSC carton 300x200x150 mm", "ea", 10000),
                ("5-ply RSC carton 400x300x200 mm", "ea", 8000),
                ("5-ply RSC carton 500x400x300 mm", "ea", 5500),
                ("7-ply heavy RSC 600x500x400 mm", "ea", 2000),
                ("7-ply heavy RSC 800x600x500 mm", "ea", 1200),
                ("3-ply die-cut mailer 250x200x50 mm", "ea", 25000),
                ("5-ply die-cut mailer 350x250x80 mm", "ea", 18000),
                ("Corrugated pallet tray 1200x800 mm", "ea", 3500),
                ("Corner guard L-profile 600 mm", "ea", 40000),
                ("Corner guard L-profile 900 mm", "ea", 28000),
                ("3-ply corrugated sheet 1200x800 mm", "sheet", 20000),
                ("5-ply corrugated sheet 1200x800 mm", "sheet", 15000),
                ("Corrugated tube OD50 x 300 mm", "ea", 9000),
                ("Corrugated tube OD75 x 300 mm", "ea", 7000),
                ("6-cell partition insert 3-ply", "ea", 5000),
                ("12-cell partition insert 3-ply", "ea", 4000),
                ("24-cell partition insert 5-ply", "ea", 2500),
                ("Telescopic outer 400x300x200 3-ply", "ea", 3500),
                ("Telescopic inner 400x300x200 3-ply", "ea", 3500),
                ("Archive box 400x300x250 3-ply", "ea", 8000),
                ("Archive box 400x300x250 5-ply", "ea", 6000),
                ("Kraft paper tape 48mm x 100m", "roll", 15000),
                ("Bubble wrap 1.2m x 50m", "roll", 2000),
                ("PE foam sheet 5mm 1200x800", "sheet", 4000),
                ("PP strapping band 12mm", "kg", 2500),
                ("Shrink film 500mm x 300m", "roll", 1200),
                ("Wooden pallet 1200x800 EUR", "ea", 800),
            ],
            start=1,
        )
    ],
    "questionnaire": [
        {"id": "Q1", "question": "Is the manufacturing site currently ISO 9001 certified?"},
        {"id": "Q2", "question": "Standard manufacturing lead time (calendar days) for this basket?"},
        {"id": "Q3", "question": "Can you support VMI / consignment for top 10 SKUs?"},
        {"id": "Q4", "question": "Minimum order quantity per SKU?"},
        {"id": "Q5", "question": "Can you supply FSC-certified board on request?"},
        {"id": "Q6", "question": "Defect / rejection rate over the last 12 months (%)?"},
        {"id": "Q7", "question": "Do you accept returns for verified quality failures within 14 days?"},
    ],
}


def _meta(quote) -> dict:
    for item in quote.raw_evidence:
        if isinstance(item, dict) and item.get("kind") == "meta":
            return item
    return {}


def summarize(label: str, quote, as_dict: dict | None = None) -> dict:
    meta = _meta(quote)
    priced = sum(1 for ln in quote.lines if ln.get("unit_price") is not None)
    return {
        "label": label,
        "vendor_id": quote.vendor_id,
        "source_format": quote.source_format,
        "parse_method": (as_dict or {}).get("parse_method") or meta.get("parse_method"),
        "vendor_name": (as_dict or {}).get("vendor_name") or meta.get("vendor_name"),
        "line_count": len(quote.lines),
        "priced_line_count": priced,
        "questionnaire_count": len(quote.questionnaire_answers),
        "confidence": quote.confidence,
        "notes_preview": (quote.notes or "")[:180],
        "sample_lines": quote.lines[:3],
    }


def main() -> None:
    results = {"ok": True, "cases": []}

    # 1) Deterministic JSON
    q1 = parse_response(SAMPLES / "V01_PackForge_response.json", "V01", RFX)
    d1 = parse_vendor_file(str(SAMPLES / "V01_PackForge_response.json"), vendor_id="V01", rfx=RFX)
    s1 = summarize("V01_json", q1, d1)
    assert "deterministic" in (s1["parse_method"] or ""), s1
    assert s1["line_count"] == 30, s1
    results["cases"].append(s1)

    # 2) Deterministic CSV
    q2 = parse_response(SAMPLES / "V02_CartonWorks_response.csv", "V02", RFX)
    d2 = parse_vendor_file(str(SAMPLES / "V02_CartonWorks_response.csv"), vendor_id="V02", rfx=RFX)
    s2 = summarize("V02_csv", q2, d2)
    assert "deterministic" in (s2["parse_method"] or ""), s2
    assert s2["line_count"] >= 20, s2
    assert s2["questionnaire_count"] >= 5, s2
    results["cases"].append(s2)

    # 3) Haiku email
    q3 = parse_response(SAMPLES / "V03_PacificBoard_email.txt", "V03", RFX)
    d3 = parse_vendor_file(str(SAMPLES / "V03_PacificBoard_email.txt"), vendor_id="V03", rfx=RFX)
    s3 = summarize("V03_email_haiku", q3, d3)
    assert "claude" in (s3["parse_method"] or "").lower(), s3
    assert s3["priced_line_count"] > 0, s3
    results["cases"].append(s3)

    # 4) Haiku prose (optional depth)
    q4 = parse_response(SAMPLES / "V04_QuickCorr_prose.txt", "V04", RFX)
    d4 = parse_vendor_file(str(SAMPLES / "V04_QuickCorr_prose.txt"), vendor_id="V04", rfx=RFX)
    s4 = summarize("V04_prose_haiku", q4, d4)
    results["cases"].append(s4)

    # 5) Haiku messy
    q5 = parse_response(SAMPLES / "V05_NestPack_messy.txt", "V05", RFX)
    d5 = parse_vendor_file(str(SAMPLES / "V05_NestPack_messy.txt"), vendor_id="V05", rfx=RFX)
    s5 = summarize("V05_messy_haiku", q5, d5)
    results["cases"].append(s5)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results, indent=2, default=str))
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()

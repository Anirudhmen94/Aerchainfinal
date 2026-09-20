"""Deterministic comparison engine. No AI in this module.

Everything numeric the buyer sees (per-piece normalisation, FX, coverage,
rankings, award totals, sensitivity) is computed here with pandas/plain Python
so it can be audited. The analyst model only *calls* these functions.

Cell status vocabulary (worst wins):
  missing       vendor did not quote the line
  unresolved    vendor referred to something we do not have ("same as last year")
  needs_review  value exists but is ambiguous, unverified, or an alternate spec
  converted     value is usable after a disclosed conversion (FX, per-100, per-kg via nominal weight)
  ok            value read directly, evidence verified
  reviewed      a buyer accepted or overrode the cell (logged)
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

SEVERITY = {"missing": 5, "unresolved": 4, "needs_review": 3, "converted": 2, "ok": 1, "reviewed": 0}
USABLE = {"ok", "converted", "reviewed"}


def fmt_inr(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    v = float(v)
    if abs(v) >= 1e7:
        return f"₹{v/1e7:,.2f} Cr"
    if abs(v) >= 1e5:
        return f"₹{v/1e5:,.2f} L"
    if abs(v) >= 1000:
        return f"₹{v:,.0f}"
    return f"₹{v:,.2f}"


def fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# Per-cell normalisation
# ---------------------------------------------------------------------------

def normalize_quote(q: dict, li: dict, fx: dict, rfx_currency: str, vendor_currency_hint: str | None) -> dict:
    """Turn an extracted quote into an INR-per-piece cell with disclosed conversions."""
    flags: list[str] = []
    notes: list[str] = []
    status = q.get("status", "needs_review")
    price = q.get("price")
    currency = (q.get("currency") or "").upper() or None
    basis = q.get("price_basis") or "unknown"
    basis_qty = q.get("basis_qty")
    unit = None

    if price is not None:
        # 1. quantity basis
        if basis == "per_pc":
            unit = float(price)
        elif basis == "per_100":
            unit = float(price) / float(basis_qty or 100)
            flags.append("per_100")
            notes.append(f"{price:g} per {int(basis_qty or 100)} pcs → ÷{int(basis_qty or 100)}")
        elif basis == "per_1000":
            unit = float(price) / float(basis_qty or 1000)
            flags.append("per_1000")
            notes.append(f"{price:g} per {int(basis_qty or 1000)} pcs → ÷{int(basis_qty or 1000)}")
        elif basis in ("per_box", "per_bundle"):
            if basis_qty and basis_qty > 0:
                unit = float(price) / float(basis_qty)
                flags.append("per_pack")
                notes.append(f"{price:g} per {basis.replace('per_','')} of {basis_qty:g} pcs → ÷{basis_qty:g}")
            else:
                flags.append("pack_size_unknown")
                notes.append(f"{price:g} per {basis.replace('per_','')}; pack size not stated, cannot convert to per piece")
                status = "needs_review"
        elif basis == "per_kg":
            w = li.get("nominal_weight_g")
            if w:
                unit = float(price) * float(w) / 1000.0
                flags.append("per_kg")
                notes.append(f"{price:g}/kg × nominal {w:g} g/pc (RSC blank {li['length_mm']}x{li['width_mm']}x{li['height_mm']} @ {li['gsm']} gsm) → {unit:.2f}")
            else:
                flags.append("weight_unknown")
                status = "needs_review"
        else:
            flags.append("basis_unknown")
            notes.append("price basis not stated")
            status = "needs_review"

        # 2. currency
        if unit is not None:
            if currency is None:
                if vendor_currency_hint:
                    currency = vendor_currency_hint
                    flags.append("currency_from_terms")
                    notes.append(f"currency taken from vendor's terms: {currency}")
                else:
                    currency = rfx_currency
                    flags.append("currency_assumed")
                    notes.append(f"currency not stated; assumed {rfx_currency} (RFx currency)")
                    if status == "ok":
                        status = "needs_review"
            if currency != "INR":
                rate = fx["rates_to_inr"].get(currency)
                if rate:
                    unit = unit * rate
                    flags.append("fx")
                    notes.append(f"{currency}→INR @ {rate:g} ({fx.get('as_of')})")
                else:
                    unit = None
                    flags.append("fx_missing")
                    notes.append(f"no FX rate for {currency}")
                    status = "needs_review"

    if status == "ok" and any(f in flags for f in ("per_100", "per_1000", "per_pack", "per_kg", "fx", "currency_from_terms")):
        status = "converted"
    if unit is not None:
        unit = round(unit, 2)
    return {
        "unit_inr": unit,
        "raw_price": price,
        "currency": currency,
        "basis": basis,
        "basis_qty": basis_qty,
        "status": status,
        "flags": flags,
        "conversion": "; ".join(notes),
        "confidence": q.get("confidence"),
        "reason": (q.get("reason") or "").strip(),
        "evidence": q.get("evidence"),
        "vendor_description": q.get("vendor_description", ""),
        "vendor_item_ref": q.get("vendor_item_ref", ""),
        "candidates": q.get("candidate_line_nos", []),
    }


# ---------------------------------------------------------------------------
# Vendor-level derived facts
# ---------------------------------------------------------------------------

def _term(ext: dict, key: str) -> dict | None:
    for t in ext.get("commercials", []):
        if t.get("key") == key:
            return t
    return None


def vendor_currency_hint(ext: dict) -> str | None:
    """A currency the vendor states globally (e.g. 'all prices in USD')."""
    text = " ".join((t.get("value") or "") + " " + (t.get("applies_to") or "") for t in ext.get("commercials", [])) + " " + (ext.get("notes") or "")
    text = text.lower()
    for code, words in {"USD": ("usd", "us dollar", "united states dollar", "us$"), "EUR": ("eur", "euro"), "GBP": ("gbp", "sterling"), "INR": ("inr", "rupee", "rs.", "rs ", "₹")}.items():
        if any(w in text for w in words):
            return code
    return None


def questionnaire_status(rfx: dict, ext: dict | None) -> dict:
    qs = rfx["questionnaire"]
    answers = {a["q_id"]: a for a in (ext or {}).get("questionnaire", [])}
    certs = (ext or {}).get("certificates", [])
    any_cert_attached = any(c.get("attached_as_document") for c in certs)
    rows = []
    knockout_failed, knockout_open = [], []
    for q in qs:
        a = answers.get(q["q_id"])
        answered = bool(a and a.get("answered"))
        ans = (a or {}).get("answer", "") if answered else ""
        verdict = "unanswered"
        if answered:
            low = ans.strip().lower()
            if q["answer_type"] == "yes_no":
                verdict = "pass" if low.startswith("y") or low in ("attached", "enclosed", "yes.") else ("fail" if low.startswith("n") or "unable" in low or "not " in low else "unclear")
            elif q["answer_type"] == "document":
                verdict = "pass" if any_cert_attached or "attached" in low or "enclosed" in low else ("claimed" if low else "unclear")
            else:
                verdict = "answered"
        if q["knockout"]:
            if verdict in ("fail",):
                knockout_failed.append(q["q_id"])
            elif verdict in ("unanswered", "unclear", "claimed"):
                knockout_open.append(q["q_id"])
        rows.append({"q_id": q["q_id"], "text": q["text"], "knockout": q["knockout"], "answered": answered, "answer": ans, "verdict": verdict, "status": (a or {}).get("status"), "evidence": (a or {}).get("evidence")})
    if ext is None:
        overall = "not_extracted"
    elif knockout_failed:
        overall = "failed"
    elif knockout_open:
        overall = "incomplete"
    else:
        overall = "cleared"
    return {"overall": overall, "knockout_failed": knockout_failed, "knockout_open": knockout_open, "answered": sum(1 for r in rows if r["answered"]), "total": len(rows), "rows": rows}


def _freight_is_extra(text: str) -> bool:
    """True when the vendor's freight/delivery wording means the price does not include delivery to the buyer."""
    import re as _re

    t = _re.sub(r"gst[^.;]*", " ", text)  # 'GST extra' must not be read as freight extra
    negative = ("freight extra", "transport extra", "freight is extra", "extra at actuals", "at actuals", "not included", "excluded", "exclusive of freight", "ex-works", "ex works", "exw", "ex-factory", "ex factory", "cif ", "fob ", "buyer's account", "buyers account", "to pay", "freight additional")
    positive = ("delivered", "included", "inclusive", "free delivery", "door delivery", "freight paid", "ddp")
    neg = any(w in t for w in negative)
    pos = any(w in t for w in positive)
    if neg:
        return True
    if pos:
        return False
    return "extra" in t and ("freight" in t or "transport" in t)


def commercial_summary(ext: dict | None) -> dict:
    if not ext:
        return {}
    out = {}
    for t in ext.get("commercials", []):
        out.setdefault(t["key"], []).append(t)
    freight = out.get("freight", []) + out.get("delivery", [])
    freight_text = " ".join((t.get("value", "") + " " + t.get("applies_to", "")) for t in freight).lower()
    disc = out.get("discount", [])
    return {
        "terms": out,
        "freight_extra": _freight_is_extra(freight_text),
        "freight_text": "; ".join(t.get("value", "") for t in freight),
        "discount_pct": next((t.get("numeric_pct") for t in disc if t.get("numeric_pct")), None),
        "discount_condition": "; ".join(f"{t.get('value','')} ({t.get('applies_to','')})".strip() for t in disc),
        "payment": "; ".join(t.get("value", "") for t in out.get("payment_terms", [])),
        "validity": "; ".join(t.get("value", "") for t in out.get("validity", [])),
        "gst": "; ".join(t.get("value", "") for t in out.get("gst", [])),
        "lead_time": "; ".join(t.get("value", "") for t in out.get("lead_time", [])),
        "moq": "; ".join(t.get("value", "") for t in out.get("moq", [])),
    }


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def _empty_cell(status: str, reason: str) -> dict:
    """A cell with every key the templates expect, for lines a vendor did not price."""
    return {
        "unit_inr": None, "raw_price": None, "currency": None, "basis": None, "basis_qty": None,
        "status": status, "flags": [], "conversion": "", "confidence": None, "reason": reason,
        "evidence": None, "vendor_description": "", "vendor_item_ref": "", "candidates": [], "quote_index": None,
    }


def build_comparison(state: dict) -> dict:
    rfx = state["rfx"]
    fx = state["fx"]
    vendors = state.get("vendors", [])
    reviews = {(r["vendor_id"], r["line_no"]): r for r in state.get("reviews", [])}
    lines = []
    for li in rfx["line_items"]:
        cells = {}
        for v in vendors:
            ext = v.get("extraction")
            if not ext:
                cells[v["vendor_id"]] = _empty_cell("not_extracted", "")
                continue
            hint = vendor_currency_hint(ext)
            matches = [(i, q) for i, q in enumerate(ext.get("line_quotes", [])) if q.get("line_no") == li["line_no"]]
            if not matches:
                cands = [(i, q) for i, q in enumerate(ext.get("line_quotes", [])) if q.get("line_no") is None and li["line_no"] in (q.get("candidate_line_nos") or [])]
                if cands:
                    i, q = cands[0]
                    cell = normalize_quote(q, li, fx, rfx["terms"]["currency"], hint)
                    cell["status"] = "needs_review"
                    cell["reason"] = (cell["reason"] + " Ambiguous row: could also be line(s) " + ", ".join(str(c) for c in q.get("candidate_line_nos", []) if c != li["line_no"])).strip()
                    cell["unit_inr_candidate"] = cell["unit_inr"]
                    cell["unit_inr"] = None
                    cell["quote_index"] = i
                else:
                    cell = _empty_cell("missing", "Not quoted")
            else:
                # Prefer the best-status match; if several map to the same line, flag it.
                matches.sort(key=lambda iq: SEVERITY.get(iq[1].get("status", "needs_review"), 3))
                i, q = matches[0]
                cell = normalize_quote(q, li, fx, rfx["terms"]["currency"], hint)
                cell["quote_index"] = i
                if len(matches) > 1:
                    cell["flags"].append("duplicate_rows")
                    cell["reason"] = (cell["reason"] + f" {len(matches)} vendor rows map to this line; showing the best-supported one.").strip()
                    if cell["status"] in USABLE:
                        cell["status"] = "needs_review"
            r = reviews.get((v["vendor_id"], li["line_no"]))
            if r:
                cell["review"] = r
                if r["action"] == "override" and r.get("value_inr") is not None:
                    cell["unit_inr"] = float(r["value_inr"])
                cell["status"] = "reviewed"
            cells[v["vendor_id"]] = cell
        usable = {vid: c["unit_inr"] for vid, c in cells.items() if c["unit_inr"] is not None and c["status"] in USABLE}
        best = min(usable.items(), key=lambda kv: kv[1]) if usable else None
        lines.append({**li, "cells": cells, "best_vendor": best[0] if best else None, "best_unit": best[1] if best else None})

    vendor_rows = []
    for v in vendors:
        ext = v.get("extraction")
        cells = [ln["cells"][v["vendor_id"]] for ln in lines]
        counts = {k: 0 for k in ("ok", "converted", "reviewed", "needs_review", "unresolved", "missing", "not_extracted")}
        for c in cells:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        usable_lines = [(ln, ln["cells"][v["vendor_id"]]) for ln in lines if ln["cells"][v["vendor_id"]]["status"] in USABLE and ln["cells"][v["vendor_id"]]["unit_inr"] is not None]
        confs = [c.get("confidence") for c in cells if c.get("confidence") is not None]
        vendor_rows.append(
            {
                "vendor_id": v["vendor_id"],
                "name": v["name"],
                "city": v.get("city"),
                "status": v.get("status"),
                "files": v.get("files", []),
                "counts": counts,
                "usable": len(usable_lines),
                "quoted": sum(1 for c in cells if c["status"] not in ("missing", "not_extracted")),
                "avg_confidence": round(sum(confs) / len(confs), 2) if confs else None,
                "extended_total_usable": round(sum(c["unit_inr"] * ln["annual_qty"] for ln, c in usable_lines), 2),
                "questionnaire": questionnaire_status(rfx, ext),
                "commercial": commercial_summary(ext),
                "certificates": (ext or {}).get("certificates", []),
                "grounding": (ext or {}).get("grounding"),
                "notes": (ext or {}).get("notes", ""),
                "currency_hint": vendor_currency_hint(ext) if ext else None,
            }
        )
    return {"rfx_id": state["id"], "title": rfx["title"], "fx": fx, "lines": lines, "vendors": vendor_rows, "line_count": len(lines)}


# ---------------------------------------------------------------------------
# Analysis functions (exposed to the analyst as tools)
# ---------------------------------------------------------------------------

def _eligible(cmp: dict, vendor_ids: list[str] | None, require_cleared: bool) -> list[dict]:
    vs = cmp["vendors"]
    if vendor_ids:
        vs = [v for v in vs if v["vendor_id"] in vendor_ids or v["name"].lower() in [x.lower() for x in vendor_ids]]
    if require_cleared:
        vs = [v for v in vs if v["questionnaire"]["overall"] == "cleared"]
    return vs


def _cell_ok(c: dict, allow_needs_review: bool) -> bool:
    if c.get("unit_inr") is None:
        return False
    if c["status"] in USABLE:
        return True
    return allow_needs_review and c["status"] == "needs_review"


def comparison_table(cmp: dict, vendor_ids: list[str] | None = None, line_nos: list[int] | None = None) -> dict:
    vs = _eligible(cmp, vendor_ids, False)
    rows = []
    for ln in cmp["lines"]:
        if line_nos and ln["line_no"] not in line_nos:
            continue
        row = {"line_no": ln["line_no"], "sku": ln["sku"], "description": ln["description"], "board": ln["board"], "annual_qty": ln["annual_qty"]}
        for v in vs:
            c = ln["cells"][v["vendor_id"]]
            row[v["name"]] = c["unit_inr"]
            row[v["name"] + " (status)"] = c["status"]
        rows.append(row)
    return {"columns": ["line_no", "sku", "description", "board", "annual_qty"] + [v["name"] for v in vs], "rows": rows, "unit": "INR per piece, normalised", "fx": cmp["fx"]}


def cheapest_per_line(cmp: dict, vendor_ids: list[str] | None = None, require_cleared_questionnaire: bool = False, allow_needs_review: bool = False) -> dict:
    vs = _eligible(cmp, vendor_ids, require_cleared_questionnaire)
    rows, uncovered = [], []
    total = 0.0
    for ln in cmp["lines"]:
        prices = [(v["name"], v["vendor_id"], ln["cells"][v["vendor_id"]]["unit_inr"]) for v in vs if _cell_ok(ln["cells"][v["vendor_id"]], allow_needs_review)]
        if not prices:
            uncovered.append(ln["line_no"])
            rows.append({"line_no": ln["line_no"], "description": ln["description"], "annual_qty": ln["annual_qty"], "winner": None, "unit_inr": None, "extended_inr": None, "runner_up": None, "runner_up_unit_inr": None})
            continue
        prices.sort(key=lambda p: p[2])
        name, vid, unit = prices[0]
        ext = round(unit * ln["annual_qty"], 2)
        total += ext
        rows.append({"line_no": ln["line_no"], "description": ln["description"], "annual_qty": ln["annual_qty"], "winner": name, "winner_id": vid, "unit_inr": unit, "extended_inr": ext, "runner_up": prices[1][0] if len(prices) > 1 else None, "runner_up_unit_inr": prices[1][2] if len(prices) > 1 else None, "gap_pct": round((prices[1][2] - unit) / unit * 100, 1) if len(prices) > 1 and unit else None})
    share: dict[str, dict] = {}
    for r in rows:
        if r["winner"]:
            s = share.setdefault(r["winner"], {"lines": 0, "extended_inr": 0.0})
            s["lines"] += 1
            s["extended_inr"] = round(s["extended_inr"] + r["extended_inr"], 2)
    return {"eligible_vendors": [v["name"] for v in vs], "rows": rows, "total_extended_inr": round(total, 2), "uncovered_lines": uncovered, "share_by_vendor": share, "allow_needs_review": allow_needs_review, "caveats": caveats_for(cmp, [v["vendor_id"] for v in vs], None, allow_needs_review)}


def vendor_totals(cmp: dict, vendor_ids: list[str] | None = None, allow_needs_review: bool = False, apply_conditional_discounts: bool = False) -> dict:
    vs = _eligible(cmp, vendor_ids, False)
    common = [ln for ln in cmp["lines"] if all(_cell_ok(ln["cells"][v["vendor_id"]], allow_needs_review) for v in vs)]
    out = []
    for v in vs:
        covered = [ln for ln in cmp["lines"] if _cell_ok(ln["cells"][v["vendor_id"]], allow_needs_review)]
        total_covered = sum(ln["cells"][v["vendor_id"]]["unit_inr"] * ln["annual_qty"] for ln in covered)
        total_common = sum(ln["cells"][v["vendor_id"]]["unit_inr"] * ln["annual_qty"] for ln in common)
        disc = v["commercial"].get("discount_pct")
        row = {
            "vendor": v["name"],
            "vendor_id": v["vendor_id"],
            "lines_covered": len(covered),
            "lines_total": cmp["line_count"],
            "total_on_covered_lines_inr": round(total_covered, 2),
            "total_on_common_lines_inr": round(total_common, 2),
            "questionnaire": v["questionnaire"]["overall"],
            "freight_extra": v["commercial"].get("freight_extra"),
            "payment": v["commercial"].get("payment"),
            "conditional_discount_pct": disc,
            "discount_condition": v["commercial"].get("discount_condition"),
        }
        if apply_conditional_discounts and disc:
            row["total_on_covered_lines_after_discount_inr"] = round(total_covered * (1 - disc / 100), 2)
            row["total_on_common_lines_after_discount_inr"] = round(total_common * (1 - disc / 100), 2)
        out.append(row)
    out.sort(key=lambda r: r["total_on_common_lines_inr"])
    return {"common_lines": [ln["line_no"] for ln in common], "common_line_count": len(common), "vendors": out, "note": "total_on_common_lines is like-for-like (only lines every listed vendor priced). total_on_covered_lines is each vendor's own coverage and is NOT comparable across vendors with different coverage.", "caveats": caveats_for(cmp, [v["vendor_id"] for v in vs], None, allow_needs_review)}


def award_scenario(cmp: dict, strategy: str = "split_cheapest", vendor_ids: list[str] | None = None, require_cleared_questionnaire: bool = False, allow_needs_review: bool = False, max_vendors: int | None = None, apply_conditional_discounts: bool = False) -> dict:
    """strategy: 'split_cheapest' | 'single_vendor' | 'split_max_n' (limit number of vendors)."""
    vs = _eligible(cmp, vendor_ids, require_cleared_questionnaire)
    if strategy == "single_vendor":
        options = []
        for v in vs:
            covered = [ln for ln in cmp["lines"] if _cell_ok(ln["cells"][v["vendor_id"]], allow_needs_review)]
            total = sum(ln["cells"][v["vendor_id"]]["unit_inr"] * ln["annual_qty"] for ln in covered)
            disc = v["commercial"].get("discount_pct") if apply_conditional_discounts else None
            options.append({"vendor": v["name"], "vendor_id": v["vendor_id"], "lines_covered": len(covered), "uncovered_lines": [ln["line_no"] for ln in cmp["lines"] if ln not in covered], "total_inr": round(total, 2), "total_after_discount_inr": round(total * (1 - disc / 100), 2) if disc else None, "questionnaire": v["questionnaire"]["overall"]})
        options.sort(key=lambda o: (-o["lines_covered"], o["total_inr"]))
        return {"strategy": strategy, "eligible_vendors": [v["name"] for v in vs], "options": options, "caveats": caveats_for(cmp, [v["vendor_id"] for v in vs], None, allow_needs_review)}
    if strategy == "split_max_n" and max_vendors:
        # brute force over vendor subsets (5 vendors → at most 31 subsets)
        from itertools import combinations
        best = None
        for k in range(1, min(max_vendors, len(vs)) + 1):
            for combo in combinations(vs, k):
                res = cheapest_per_line(cmp, [v["vendor_id"] for v in combo], False, allow_needs_review)
                key = (len(res["uncovered_lines"]), res["total_extended_inr"])
                if best is None or key < best[0]:
                    best = (key, res, [v["name"] for v in combo])
        if best:
            res = best[1]
            res["strategy"] = strategy
            res["chosen_vendors"] = best[2]
            return res
    res = cheapest_per_line(cmp, [v["vendor_id"] for v in vs], False, allow_needs_review)
    res["strategy"] = "split_cheapest"
    if apply_conditional_discounts:
        adj = {}
        for name, s in res["share_by_vendor"].items():
            v = next(v for v in cmp["vendors"] if v["name"] == name)
            disc = v["commercial"].get("discount_pct")
            adj[name] = round(s["extended_inr"] * (1 - disc / 100), 2) if disc else s["extended_inr"]
        res["share_after_conditional_discounts_inr"] = adj
        res["total_after_conditional_discounts_inr"] = round(sum(adj.values()), 2)
    return res


def sensitivity(cmp: dict, vendor_id: str, pct_change: float, require_cleared_questionnaire: bool = False, allow_needs_review: bool = False) -> dict:
    """Re-run cheapest-per-line with one vendor's prices shifted by pct_change (e.g. +10)."""
    import copy
    before = cheapest_per_line(cmp, None, require_cleared_questionnaire, allow_needs_review)
    cmp2 = copy.deepcopy(cmp)
    target = next((v for v in cmp2["vendors"] if v["vendor_id"] == vendor_id or v["name"].lower() == vendor_id.lower()), None)
    if not target:
        raise ValueError(f"unknown vendor {vendor_id}")
    for ln in cmp2["lines"]:
        c = ln["cells"][target["vendor_id"]]
        if c.get("unit_inr") is not None:
            c["unit_inr"] = round(c["unit_inr"] * (1 + pct_change / 100), 2)
    after = cheapest_per_line(cmp2, None, require_cleared_questionnaire, allow_needs_review)
    changed = [{"line_no": a["line_no"], "before": b["winner"], "after": a["winner"]} for a, b in zip(after["rows"], before["rows"]) if a["winner"] != b["winner"]]
    return {"vendor": target["name"], "pct_change": pct_change, "total_before_inr": before["total_extended_inr"], "total_after_inr": after["total_extended_inr"], "lines_that_change_winner": changed, "share_before": before["share_by_vendor"], "share_after": after["share_by_vendor"]}


def list_flags(cmp: dict, vendor_ids: list[str] | None = None, statuses: list[str] | None = None) -> dict:
    vs = _eligible(cmp, vendor_ids, False)
    statuses = statuses or ["needs_review", "unresolved", "missing", "converted"]
    out = []
    for ln in cmp["lines"]:
        for v in vs:
            c = ln["cells"][v["vendor_id"]]
            if c["status"] in statuses:
                out.append({"line_no": ln["line_no"], "vendor": v["name"], "status": c["status"], "flags": c.get("flags", []), "reason": c.get("reason", ""), "conversion": c.get("conversion", ""), "raw_price": c.get("raw_price"), "currency": c.get("currency"), "basis": c.get("basis"), "unit_inr": c.get("unit_inr"), "confidence": c.get("confidence")})
    return {"count": len(out), "items": out}


def questionnaire_overview(cmp: dict) -> dict:
    return {v["name"]: {"overall": v["questionnaire"]["overall"], "answered": f"{v['questionnaire']['answered']}/{v['questionnaire']['total']}", "knockout_failed": v["questionnaire"]["knockout_failed"], "knockout_open": v["questionnaire"]["knockout_open"], "certificates": [{"name": c["name"], "attached": c.get("attached_as_document"), "claimed_only": c.get("claimed_in_text") and not c.get("attached_as_document")} for c in v["certificates"]]} for v in cmp["vendors"]}


def commercial_terms(cmp: dict) -> dict:
    return {v["name"]: {k: v["commercial"].get(k) for k in ("payment", "validity", "gst", "lead_time", "moq", "freight_text", "freight_extra", "discount_pct", "discount_condition")} for v in cmp["vendors"]}


def caveats_for(cmp: dict, vendor_ids: list[str] | None, line_nos: list[int] | None, allow_needs_review: bool) -> list[str]:
    """Human-readable caveats about the data a computation relied on or excluded."""
    vs = _eligible(cmp, vendor_ids, False)
    out = []
    for v in vs:
        c = v["counts"]
        parts = []
        if c.get("missing"):
            parts.append(f"{c['missing']} line(s) not quoted")
        if c.get("unresolved"):
            parts.append(f"{c['unresolved']} line(s) unresolved (excluded)")
        if c.get("needs_review"):
            parts.append(f"{c['needs_review']} line(s) need review ({'INCLUDED at your request' if allow_needs_review else 'excluded'})")
        if c.get("converted"):
            parts.append(f"{c['converted']} line(s) rely on disclosed conversions (FX / per-100 / per-kg via nominal weight)")
        if v["commercial"].get("freight_extra"):
            parts.append("freight is EXTRA (not in prices)")
        if v["commercial"].get("discount_pct"):
            parts.append(f"conditional discount {v['commercial']['discount_pct']:g}% not applied unless requested ({v['commercial'].get('discount_condition','')})")
        if v["questionnaire"]["overall"] != "cleared":
            parts.append(f"questionnaire {v['questionnaire']['overall']}")
        if parts:
            out.append(f"{v['name']}: " + "; ".join(parts) + ".")
    out.append(f"FX: {', '.join(f'{k} {v:g}' for k, v in cmp['fx']['rates_to_inr'].items() if k != 'INR')} to INR as of {cmp['fx']['as_of']} ({cmp['fx']['source']}).")
    return out


def to_dataframe(cmp: dict) -> pd.DataFrame:
    rows = []
    for ln in cmp["lines"]:
        for v in cmp["vendors"]:
            c = ln["cells"][v["vendor_id"]]
            rows.append({"line_no": ln["line_no"], "sku": ln["sku"], "description": ln["description"], "board": ln["board"], "annual_qty": ln["annual_qty"], "vendor": v["name"], "unit_inr": c.get("unit_inr"), "status": c["status"], "raw_price": c.get("raw_price"), "currency": c.get("currency"), "basis": c.get("basis"), "conversion": c.get("conversion"), "confidence": c.get("confidence"), "reason": c.get("reason")})
    return pd.DataFrame(rows)

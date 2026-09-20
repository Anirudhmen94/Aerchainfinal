"""Deterministic quality questionnaire knockout gate. No LLM."""
from __future__ import annotations

import re
from typing import Any, Optional

from shared_models import (
    ExtractedQuote,
    KnockoutResult,
    QuestionnaireItem,
    VendorQualification,
)


_PASS = {
    "yes", "y", "true", "1", "pass", "passed", "ok", "available",
    "certified", "compliant", "provided", "attached", "enclosed",
    "have", "we have", "we do", "capable", "can provide",
}
_FAIL = {
    "no", "n", "false", "0", "fail", "failed", "none", "na", "n/a",
    "not available", "not certified", "unable", "cannot", "we cannot",
    "not provided", "missing",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_answer(value: Any) -> str:
    return " ".join(_text(value).lower().split())


def _answer_passed(raw: Any) -> Optional[bool]:
    """True / False / None (unclear or blank)."""
    text = _norm_answer(raw)
    if not text:
        return None
    unclear = (
        "not stated", "not mentioned", "not specified", "unknown", "unclear",
        "n/a", "na", "tbd", "to be confirmed", "see attached", "refer",
    )
    if text in unclear or text.startswith("not stated") or text.startswith("not mentioned"):
        return None
    if text in _PASS or text.startswith("yes") or text.startswith("y "):
        return True
    if (
        text in _FAIL
        or text.startswith("no")
        or "not certified" in text
        or "unable" in text
        or "cannot" in text
    ):
        return False
    # numeric / free-text answers to non-yes-no knockouts: treat as present → pass
    return True


def _index_answers(answers: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in answers or []:
        qid = _text(_get(row, "id", _get(row, "question_id", _get(row, "q_id"))))
        if not qid:
            continue
        ans = _get(row, "answer", _get(row, "value", _get(row, "response")))
        out[qid] = ans
        out[qid.lower()] = ans
    return out


def _questionnaire_items(rfx: Any) -> list[Any]:
    return list(_get(rfx, "questionnaire", []) or [])

def _norm_qtext(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _q_tokens(s: str) -> set[str]:
    stop = {
        "the", "and", "for", "you", "your", "are", "is", "this", "that",
        "with", "from", "have", "will", "can", "into", "within", "over",
        "last", "days", "day", "per", "any", "all", "please", "provide",
        "do", "does", "what", "how", "when", "who", "which",
    }
    return {t for t in _norm_qtext(s).split() if len(t) > 2 and t not in stop}


def remap_questionnaire_answers(rfx: Any, answers: list[Any] | None) -> list[dict[str, Any]]:
    """Map parser answers onto live RFx questionnaire ids (by id, then question text).

    Mirrors agents.document_parser._quality_gate_answers so qualify/normalize see
    the same alignment the parser applies when an RFx context is present.
    Never invents answers. Ensures every RFx question appears once.
    """
    import re as _re  # local; module-level re added below
    rfx_qs = _questionnaire_items(rfx)
    if not rfx_qs:
        # No live questionnaire — pass through normalized shape
        out = []
        for row in answers or []:
            if not isinstance(row, dict) and not hasattr(row, "id"):
                continue
            out.append({
                "id": _text(_get(row, "id", _get(row, "question_id", ""))),
                "question": _text(_get(row, "question", "")),
                "answer": _text(_get(row, "answer", _get(row, "value", ""))),
                "answered": bool(_text(_get(row, "answer", _get(row, "value", "")))),
            })
        return out

    def _ceil_half(n: int) -> int:
        return (n + 1) // 2

    def _match_rfx(qid: str, qtext: str, used: set[str]):
        if qid:
            for rq in rfx_qs:
                rq_id = _text(_get(rq, "id"))
                if rq_id and rq_id.upper() == qid.upper() and rq_id.upper() not in used:
                    return rq
        qn = _norm_qtext(qtext)
        if qn:
            for rq in rfx_qs:
                rq_id = _text(_get(rq, "id"))
                if rq_id.upper() in used:
                    continue
                rn = _norm_qtext(_text(_get(rq, "question")))
                if not rn:
                    continue
                if qn == rn or qn in rn or rn in qn:
                    return rq
        q_tokens = _q_tokens(qtext)
        if len(q_tokens) >= 3:
            best = None
            best_score = 0
            for rq in rfx_qs:
                rq_id = _text(_get(rq, "id"))
                if rq_id.upper() in used:
                    continue
                r_tokens = _q_tokens(_text(_get(rq, "question")))
                if len(r_tokens) < 3:
                    continue
                score = len(q_tokens & r_tokens)
                smaller = min(len(q_tokens), len(r_tokens))
                if score >= 3 and score >= _ceil_half(smaller) and score > best_score:
                    best_score = score
                    best = rq
            return best
        return None

    shaped: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for raw in answers or []:
        if raw is None:
            continue
        qid = _text(_get(raw, "id", _get(raw, "question_id", _get(raw, "q_id"))))
        qtext = _text(_get(raw, "question", _get(raw, "q", _get(raw, "text", ""))))
        ans = _text(_get(raw, "answer", _get(raw, "value", _get(raw, "response", ""))))
        matched = _match_rfx(qid, qtext, used_ids)
        if matched is None:
            # Keep orphan only if it has content (debug); skip blank orphans
            if ans or qtext:
                shaped.append({
                    "id": qid,
                    "question": qtext,
                    "answer": ans,
                    "answered": bool(ans),
                    "orphaned": True,
                })
            continue
        mid = _text(_get(matched, "id"))
        used_ids.add(mid.upper())
        shaped.append({
            "id": mid,
            "question": _text(_get(matched, "question")) or qtext,
            "answer": ans,
            "answered": bool(ans),
        })

    # Ensure every live RFx question is represented
    have = {(_text(r.get("id"))).upper() for r in shaped if r.get("id")}
    for rq in rfx_qs:
        rid = _text(_get(rq, "id"))
        if rid.upper() in have:
            continue
        shaped.append({
            "id": rid,
            "question": _text(_get(rq, "question")),
            "answer": "",
            "answered": False,
        })
    return shaped


def _index_answers_for_rfx(rfx: Any, answers: list[Any] | None) -> dict[str, Any]:
    """Index remapped answers by live RFx question id (and lowercase)."""
    remapped = remap_questionnaire_answers(rfx, answers)
    out: dict[str, Any] = {}
    for row in remapped:
        if row.get("orphaned"):
            continue
        qid = _text(row.get("id"))
        if not qid:
            continue
        out[qid] = row.get("answer")
        out[qid.lower()] = row.get("answer")
    return out


def qualify_vendor(rfx: Any, extraction: ExtractedQuote | dict) -> VendorQualification:
    """Pass/fail a single vendor against RFx knockout questions."""
    vendor_id = _text(_get(extraction, "vendor_id")) or "unknown-vendor"
    answers = _index_answers_for_rfx(rfx, _get(extraction, "questionnaire_answers", []) or [])
    results: list[KnockoutResult] = []
    reasons: list[str] = []

    knockouts = [q for q in _questionnaire_items(rfx) if bool(_get(q, "knockout", False))]
    if not knockouts:
        # No knockout gate configured → every quoting vendor is eligible.
        return VendorQualification(
            vendor_id=vendor_id,
            passed=True,
            award_eligible=True,
            knockout_results=[],
            reasons=["No knockout questions on RFx"],
        )

    all_passed = True
    for q in knockouts:
        qid = _text(_get(q, "id"))
        question = _text(_get(q, "question"))
        raw = answers.get(qid, answers.get(qid.lower()))
        verdict = _answer_passed(raw)
        if verdict is True:
            results.append(KnockoutResult(
                question_id=qid, question=question,
                answer=_text(raw) or None, passed=True, reason="Accepted",
            ))
        elif verdict is False:
            all_passed = False
            reason = f"Failed knockout: {qid}"
            reasons.append(reason)
            results.append(KnockoutResult(
                question_id=qid, question=question,
                answer=_text(raw) or None, passed=False, reason=reason,
            ))
        else:
            all_passed = False
            reason = f"Missing/unclear knockout answer: {qid}"
            reasons.append(reason)
            results.append(KnockoutResult(
                question_id=qid, question=question,
                answer=_text(raw) or None, passed=False, reason=reason,
            ))

    return VendorQualification(
        vendor_id=vendor_id,
        passed=all_passed,
        award_eligible=all_passed,
        knockout_results=results,
        reasons=reasons,
    )


def qualify_vendors(rfx: Any, extractions: list[ExtractedQuote] | list[dict]) -> list[VendorQualification]:
    return [qualify_vendor(rfx, ext) for ext in extractions]


def award_eligible_vendors(rfx: Any, extractions: list[ExtractedQuote] | list[dict]) -> list[str]:
    return [q.vendor_id for q in qualify_vendors(rfx, extractions) if q.award_eligible]


__all__ = [
    "award_eligible_vendors",
    "build_questionnaire_results",
    "eligibility_gaps",
    "qualify_vendor",
    "qualify_vendors",
    "questionnaire_matrix",
    "remap_questionnaire_answers",
]


def build_questionnaire_results(
    rfx: Any,
    extractions: list[ExtractedQuote] | list[dict],
) -> dict[str, list]:
    """Full per-vendor QuestionnaireResult lists (all RFx questions, not only knockouts)."""
    from shared_models import QuestionnaireResult

    items = _questionnaire_items(rfx)
    out: dict[str, list] = {}
    for extraction in extractions:
        vendor_id = _text(_get(extraction, "vendor_id")) or "unknown-vendor"
        answers = _index_answers_for_rfx(rfx, _get(extraction, "questionnaire_answers", []) or [])
        rows = []
        for q in items:
            qid = _text(_get(q, "id"))
            question = _text(_get(q, "question"))
            knockout = bool(_get(q, "knockout", False))
            raw = answers.get(qid, answers.get(qid.lower()))
            verdict = _answer_passed(raw)
            rows.append(QuestionnaireResult(
                question_id=qid,
                question=question,
                knockout=knockout,
                answer=_text(raw),
                passed=verdict,  # None if unanswered
            ))
        out[vendor_id] = rows
    return out


def questionnaire_matrix(rfx: Any, comparison: Any) -> list[dict[str, Any]]:
    """UI rows for Compare tab: one row per RFx question × vendor cells.

    Signature:
        questionnaire_matrix(rfx, comparison) -> list[dict]

    Each row:
        {
          "question_id": str,
          "question": str,
          "knockout": bool,
          "by_vendor": {
            vendor_id: {
              "vendor_name": str,
              "answer": str,
              "passed": bool | None,
              "status": "pass" | "fail" | "unanswered",
              "award_eligible": bool,
            },
            ...
          },
        }

    Column order follows comparison.award_eligible_vendors then remaining vendors
    (or RFx vendor order when comparison has no vendor_names yet).
    """
    items = _questionnaire_items(rfx)
    q_results = _get(comparison, "questionnaire_results", {}) or {}
    vendor_names = dict(_get(comparison, "vendor_names", {}) or {})
    eligible = set(_get(comparison, "award_eligible_vendors", []) or [])
    # Prefer RFx vendor order; fall back to keys present in results.
    rfx_vendors = [
        _text(_get(v, "vendor_id"))
        for v in (_get(rfx, "vendors", []) or [])
        if _text(_get(v, "vendor_id"))
    ]
    for vid in rfx_vendors:
        if vid and vid not in vendor_names:
            for v in (_get(rfx, "vendors", []) or []):
                if _text(_get(v, "vendor_id")) == vid:
                    vendor_names[vid] = _text(_get(v, "name")) or vid
                    break
    extra = [vid for vid in q_results.keys() if vid not in rfx_vendors]
    vendor_ids = rfx_vendors + extra

    # Index results: vendor -> question_id -> QuestionnaireResult-like
    indexed: dict[str, dict[str, Any]] = {}
    for vid, rows in q_results.items():
        bucket: dict[str, Any] = {}
        for row in rows or []:
            qid = _text(_get(row, "question_id"))
            if qid:
                bucket[qid] = row
                bucket[qid.lower()] = row
        indexed[vid] = bucket

    rows_out: list[dict[str, Any]] = []
    for q in items:
        qid = _text(_get(q, "id"))
        question = _text(_get(q, "question"))
        knockout = bool(_get(q, "knockout", False))
        by_vendor: dict[str, Any] = {}
        for vid in vendor_ids:
            cell = indexed.get(vid, {}).get(qid) or indexed.get(vid, {}).get(qid.lower())
            answer = _text(_get(cell, "answer")) if cell is not None else ""
            passed = _get(cell, "passed") if cell is not None else None
            if passed is True:
                status = "pass"
            elif passed is False:
                status = "fail"
            else:
                status = "unanswered"
            by_vendor[vid] = {
                "vendor_name": vendor_names.get(vid, vid),
                "answer": answer,
                "passed": passed,
                "status": status,
                "award_eligible": vid in eligible,
            }
        rows_out.append({
            "question_id": qid,
            "question": question,
            "knockout": knockout,
            "by_vendor": by_vendor,
        })
    return rows_out


def eligibility_gaps(
    rfx: Any,
    comparison: Any,
    line_id: str | None = None,
) -> dict[str, Any]:
    """Explain why Award has few/no eligible vendors.

    Signature:
        eligibility_gaps(rfx, comparison, line_id=None) -> dict

    Returns:
        {
          "line_id": str | None,
          "award_eligible_vendors": list[str],
          "summary": str,
          "vendors": [
            {
              "vendor_id": str,
              "vendor_name": str,
              "award_eligible": bool,
              "questionnaire_passed": bool,
              "failed_knockouts": [{"question_id","question","answer","reason"}],
              "missing_knockouts": [{"question_id","question","reason"}],
              "line": {  # only when line_id set
                "status": str | None,
                "unit_price_inr": float | None,
                "has_price": bool,
                "blockers": list[str],  # no_price | missing | uom_mismatch | uncertain | not_award_eligible
              } | None,
              "blockers": list[str],
            },
            ...
          ],
          "lines": [  # when line_id is None: per-line eligible count
            {"line_id", "eligible_with_price": list[str], "n_eligible_with_price": int}
          ],
        }
    """
    quals = list(_get(comparison, "qualifications", []) or [])
    qual_by = {_text(_get(q, "vendor_id")): q for q in quals}
    eligible = list(_get(comparison, "award_eligible_vendors", []) or [])
    vendor_names = dict(_get(comparison, "vendor_names", {}) or {})
    for v in (_get(rfx, "vendors", []) or []):
        vid = _text(_get(v, "vendor_id"))
        if vid and vid not in vendor_names:
            vendor_names[vid] = _text(_get(v, "name")) or vid

    cells = list(_get(comparison, "cells", []) or [])
    cells_by: dict[tuple[str, str], Any] = {}
    for c in cells:
        cells_by[(_text(_get(c, "line_id")), _text(_get(c, "vendor_id")))] = c

    line_ids = []
    if line_id:
        line_ids = [_text(line_id)]
    else:
        seen = []
        for li in (_get(rfx, "line_items", []) or []):
            lid = _text(_get(li, "line_id"))
            if lid and lid not in seen:
                seen.append(lid)
        if not seen:
            for c in cells:
                lid = _text(_get(c, "line_id"))
                if lid and lid not in seen:
                    seen.append(lid)
        line_ids = seen

    vendor_ids = []
    for vid in list(qual_by.keys()) + list(vendor_names.keys()):
        if vid and vid not in vendor_ids:
            vendor_ids.append(vid)
    for c in cells:
        vid = _text(_get(c, "vendor_id"))
        if vid and vid not in vendor_ids:
            vendor_ids.append(vid)

    usable = {"ok", "converted"}
    vendors_out = []
    for vid in vendor_ids:
        qual = qual_by.get(vid)
        passed = bool(_get(qual, "award_eligible", False)) if qual else (vid in eligible)
        failed_kos, missing_kos = [], []
        for kr in (_get(qual, "knockout_results", []) or []):
            if _get(kr, "passed") is True:
                continue
            entry = {
                "question_id": _text(_get(kr, "question_id")),
                "question": _text(_get(kr, "question")),
                "answer": _text(_get(kr, "answer")) or None,
                "reason": _text(_get(kr, "reason")),
            }
            ans = _text(_get(kr, "answer"))
            if not ans or "missing" in _norm_answer(_get(kr, "reason")) or "unclear" in _norm_answer(_get(kr, "reason")):
                missing_kos.append(entry)
            else:
                failed_kos.append(entry)

        blockers: list[str] = []
        if not passed:
            if failed_kos:
                blockers.append("failed_knockouts")
            if missing_kos:
                blockers.append("missing_knockouts")
            if not failed_kos and not missing_kos:
                blockers.append("not_award_eligible")

        line_info = None
        if line_id is not None:
            lid = _text(line_id)
            cell = cells_by.get((lid, vid))
            status = _text(_get(cell, "status")) if cell is not None else "missing"
            price = _get(cell, "unit_price_inr") if cell is not None else None
            has_price = price is not None and status in usable
            line_blockers = []
            if not passed:
                line_blockers.append("not_award_eligible")
            if cell is None or status == "missing" or price is None:
                line_blockers.append("no_price" if status != "missing" else "missing")
            elif status == "uom_mismatch":
                line_blockers.append("uom_mismatch")
            elif status == "uncertain":
                line_blockers.append("uncertain")
            line_info = {
                "status": status or None,
                "unit_price_inr": price,
                "has_price": has_price,
                "blockers": line_blockers,
            }
            blockers = list(dict.fromkeys(blockers + line_blockers))

        vendors_out.append({
            "vendor_id": vid,
            "vendor_name": vendor_names.get(vid, vid),
            "award_eligible": passed,
            "questionnaire_passed": passed,
            "failed_knockouts": failed_kos,
            "missing_knockouts": missing_kos,
            "line": line_info,
            "blockers": blockers,
            "reasons": list(_get(qual, "reasons", []) or []) if qual else [],
        })

    lines_out = []
    n_pass = len(eligible)
    failed_ko_ids: list[str] = []
    for v in vendors_out:
        for kr in v.get("failed_knockouts") or []:
            qid = _text(kr.get("question_id"))
            if qid and qid not in failed_ko_ids:
                failed_ko_ids.append(qid)
        for kr in v.get("missing_knockouts") or []:
            qid = _text(kr.get("question_id"))
            if qid and qid not in failed_ko_ids:
                failed_ko_ids.append(qid)

    for lid in line_ids:
        ok_vids: list[str] = []
        status_counts: dict[str, int] = {}
        pass_no_price = 0
        for vid in eligible:
            cell = cells_by.get((lid, vid))
            if cell is None:
                pass_no_price += 1
                status_counts["missing"] = status_counts.get("missing", 0) + 1
                continue
            status = _text(_get(cell, "status")) or "missing"
            price = _get(cell, "unit_price_inr")
            status_counts[status] = status_counts.get(status, 0) + 1
            if price is not None and status in usable:
                ok_vids.append(vid)
            else:
                pass_no_price += 1

        reasons: list[str] = []
        if n_pass == 0:
            reasons.append("0 Pass vendors")
            if failed_ko_ids:
                reasons.append("failed knockouts: " + ", ".join(failed_ko_ids))
        elif len(ok_vids) == 0:
            reasons.append(f"{n_pass} Pass but no usable price on this line")
            ugly = [s for s in ("missing", "uom_mismatch", "uncertain") if status_counts.get(s)]
            if ugly:
                bits = [f"{status_counts[s]} {s}" for s in ugly]
                reasons.append("cell status " + "/".join(bits))
        lines_out.append({
            "line_id": lid,
            "eligible_with_price": ok_vids,
            "n_eligible_with_price": len(ok_vids),
            "pass_no_price": pass_no_price,
            "status_counts": status_counts,
            "reasons": reasons,
        })

    n_elig = len(eligible)
    n_lines_zero = sum(1 for row in lines_out if row["n_eligible_with_price"] == 0)
    if n_elig == 0:
        summary = (
            f"No award-eligible vendors: all {len(vendors_out)} failed or missed "
            f"knockout questionnaire items."
        )
        ko_note = (", ".join(failed_ko_ids) if failed_ko_ids else "see Compare questionnaire matrix")
        global_banner = (
            f"Shortlist (Pass) is empty — every vendor failed or left unanswered knockouts "
            f"({ko_note}). Award dropdowns stay empty until knockouts clear."
        )
    elif line_id:
        row = next((r for r in lines_out if r["line_id"] == _text(line_id)), None)
        n = row["n_eligible_with_price"] if row else 0
        summary = (
            f"{n_elig} award-eligible vendor(s); {n} with a usable price on line {line_id}."
        )
        global_banner = None
    else:
        summary = (
            f"{n_elig} award-eligible vendor(s); "
            f"{n_lines_zero}/{len(lines_out)} lines have zero eligible vendors with price."
        )
        global_banner = None

    return {
        "line_id": _text(line_id) if line_id is not None else None,
        "award_eligible_vendors": list(eligible),
        "summary": summary,
        "global_banner": global_banner,
        "failed_knockout_ids": failed_ko_ids,
        "vendors": vendors_out,
        "lines": lines_out if line_id is None else [r for r in lines_out if r["line_id"] == _text(line_id)],
        "reasons_by_line": {
            r["line_id"]: r.get("reasons") or []
            for r in lines_out
            if r.get("reasons")
        },
    }



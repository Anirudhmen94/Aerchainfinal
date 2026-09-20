"""Deterministic quality questionnaire knockout gate. No LLM."""
from __future__ import annotations

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
    if text in _PASS or text.startswith("yes") or text.startswith("y "):
        return True
    if text in _FAIL or text.startswith("no") or "not certified" in text or "unable" in text:
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


def qualify_vendor(rfx: Any, extraction: ExtractedQuote | dict) -> VendorQualification:
    """Pass/fail a single vendor against RFx knockout questions."""
    vendor_id = _text(_get(extraction, "vendor_id")) or "unknown-vendor"
    answers = _index_answers(_get(extraction, "questionnaire_answers", []) or [])
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
    "qualify_vendor",
    "qualify_vendors",
]

"""Agent 1 — RFx Drafter (LIVE PRODUCT wizard Draft stage).

Turns a plain-language buyer brief into a validated shared_models.RFx using
Anthropic Claude Haiku with forced tool-use for structured output.

Public API
----------
- draft_rfx(brief, **kwargs) -> RFx
- regenerate_line_items(rfx, brief=None, **kwargs) -> RFx
- apply_rfx_edits(rfx, **edits) -> RFx
- nominal_weight_g(length_mm, width_mm, height_mm, gsm) -> float

Manual-edit overlays (title/scope/terms/currency/vendors) always win over
model output when the caller supplies them. regenerate_line_items preserves
existing rfx_id/title/scope/terms/currency by discarding those fields from
the model response (one shared emit_rfx tool schema).
"""
from __future__ import annotations

import re

import hashlib
import os
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from shared_models import LineItem, QuestionnaireItem, RFx, Vendor

DEFAULT_HAIKU_MODEL = "claude-3-haiku-20240307"


def infer_line_count(brief: str, *, default: int = 30, minimum: int = 1, maximum: int = 60) -> int:
    """Infer requested SKU / line-item count from the buyer brief.

    Examples: "~10 SKUs", "10 line items", "about 15 SKUs". Defaults to 30
    (assignment demo size) when the brief does not name a count.
    """
    if not brief:
        return default
    patterns = [
        r"(?:~|about|around|approx(?:imately)?\s*)?(\d{1,2})\s*SKUs?\b",
        r"(?:~|about|around)?\s*(\d{1,2})\s*line\s*items?\b",
        r"(?:~|about|around)?\s*(\d{1,2})\s*SKU\b",
        r"\b(\d{1,2})\s*SKUs?\b",
    ]
    for pat in patterns:
        m = re.search(pat, brief, flags=re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return max(minimum, min(maximum, n))
    return default


def _align_prose_to_line_count(text: str, target_lines: int, brief: str) -> str:
    """Keep scope/title prose consistent with the brief's SKU count.

    Models often emit "30 SKUs" in scope even when asked for 15. Rewrite
    countable phrases to match target_lines without inventing new commercial terms.
    """
    if not text:
        return text
    n = int(target_lines)
    out = text
    patterns = [
        (r"(?:~|about|around|approximately)\s*\d{1,2}\s*SKUs?", f"~{n} SKUs"),
        (r"\b\d{1,2}\s*SKUs?\b", f"{n} SKUs"),
        (r"\b\d{1,2}\s*line\s*items?\b", f"{n} line items"),
        (r"\b\d{1,2}\s*SKU\b", f"{n} SKU"),
        (r"(?:a\s+)?(?:full\s+)?set\s+of\s+\d{1,2}\s+line\s+items", f"a set of {n} line items"),
    ]
    for pat, repl in patterns:
        out = re.sub(pat, repl, out, flags=re.IGNORECASE)
    # Append once only when the brief explicitly named this count and prose has none
    brief_n = infer_line_count(brief, default=-1)
    if (
        brief_n == n
        and brief_n > 0
        and not re.search(r"\bSKUs?\b|line\s*items?", out, flags=re.IGNORECASE)
    ):
        out = out.rstrip() + f" This RFx covers {n} SKUs as stated in the buyer brief."
    return out


def _align_rfx_prose(
    rfx: RFx,
    target_lines: int,
    brief: str,
    *,
    fields: tuple[str, ...] = ("title", "scope", "terms"),
) -> RFx:
    """Rewrite countable SKU phrases in selected prose fields."""
    update: dict[str, str] = {}
    for key in fields:
        val = getattr(rfx, key, None)
        if isinstance(val, str):
            update[key] = _align_prose_to_line_count(val, target_lines, brief)
    return rfx.model_copy(update=update) if update else rfx


TOOL_NAME = "emit_rfx"

# Realistic India corrugated shortlist — pad/seed when the model omits vendors
# or returns a list that cannot be coerced. Always ends at length 5.
_SEED_VENDORS: list[Vendor] = [
    Vendor(vendor_id="V1", name="Sri Balaji Packaging Pvt Ltd", email="quotes@sribalaji-packaging.in"),
    Vendor(vendor_id="V2", name="Kraftline Corrugators", email="rfq@kraftline.co.in"),
    Vendor(vendor_id="V3", name="PakAsia Export Packaging", email="sales@pakasia.in"),
    Vendor(vendor_id="V4", name="Meghna Boxes & Boards", email="rates@meghnaboxes.in"),
    Vendor(vendor_id="V5", name="Ganesh Paper Products", email="enquiry@ganeshpaper.in"),
]

SYSTEM_PROMPT = """You are a senior category manager for packaging procurement in India.
Draft a procurement-grade RFx (request for quotation) for corrugated packaging
from the buyer's plain-language brief.

Hard rules:
- Emit exactly N line items via the emit_rfx tool (N is given in the user message; default 30 only when the brief does not specify a count). Mix 3-ply and 5-ply RSC /
  die-cut cartons (at most two 7-ply only if the brief warrants). Dimensions in
  mm, board GSM, flute (B/C/BC/E), print spec, and annual volumes in pieces.
- Put engineering detail in each line's specs dict (keys such as length_mm,
  width_mm, height_mm, ply, gsm, flute, print). Descriptions must be specific
  enough for a vendor to quote without calling back.
- Questionnaire: emit EXACTLY 8 questions (not fewer). Cover these topics in order:
  Q1 ISO 9001 (knockout), Q2 FSC / chain-of-custody (knockout), Q3 food-contact /
  hygiene certification (knockout), Q4 in-house BCT/ECT or burst testing,
  Q5 installed capacity / utilisation, Q6 lead time to first delivery,
  Q7 moisture/contamination control for snacks packaging, Q8 references from
  food/snacks buyers. Mark exactly 3 or 4 as knockout=true (a 'No' disqualifies).
- Commercial terms as a single clear string: INR per piece, delivered,
  exclusive of GST unless the brief says otherwise; payment, validity, freight.
- Follow the brief's numbers (volumes, sizes, plant, timelines). Never invent
  supplier unit prices. Do not contradict the brief.
- Default category framing: corrugated packaging India. Currency INR.
- Provide a shortlist of exactly 5 Indian corrugated vendors (vendor_id, name,
  email). If unsure of real emails, use plausible professional addresses.
- Call emit_rfx once with the complete draft. No prose outside the tool.
"""


class _DraftLine(BaseModel):
    line_id: str = Field(description="Stable id, e.g. L01")
    description: str
    qty: float = Field(description="Annual quantity in pieces")
    uom: str = "piece"
    specs: dict[str, Any] = Field(default_factory=dict)


class _DraftQuestion(BaseModel):
    id: str = Field(description="e.g. Q1")
    question: str
    knockout: bool = False


class _DraftVendor(BaseModel):
    vendor_id: str
    name: str
    email: str


class _DraftPayload(BaseModel):
    """Structured RFx draft emitted by the model via forced tool use."""

    title: str
    scope: str = Field(
        description="Background, what is sourced, volumes, quality, submission instructions"
    )
    terms: str = Field(description="Commercial terms as a single prose block")
    currency: str = "INR"
    line_items: list[_DraftLine] = Field(description="Corrugated line items; count must match requested N")
    questionnaire: list[_DraftQuestion] = Field(description="Exactly 8 questions; 3–4 knockout")
    vendors: list[_DraftVendor] = Field(
        default_factory=list, description="Exactly 5 target vendors"
    )


class RFxDraftError(RuntimeError):
    """Raised when the model cannot produce a valid 30-line RFx after retry."""


def nominal_weight_g(length_mm: int, width_mm: int, height_mm: int, gsm: int) -> float:
    """Approximate RSC blank weight (g) from L/W/H mm and board GSM.

    Blank length = 2*(L+W)+40 mm glue flap; blank height = H+W.
    Optional helper for later per-kg → per-piece conversion.
    """
    blank_len = 2 * (length_mm + width_mm) + 40
    blank_h = height_mm + width_mm
    area_m2 = (blank_len * blank_h) / 1_000_000
    return round(area_m2 * gsm, 1)


def _haiku_model() -> str:
    return os.environ.get("ANTHROPIC_HAIKU_MODEL") or DEFAULT_HAIKU_MODEL


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RFxDraftError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment to draft an RFx."
        )
    return key


def _make_client(client: Any | None = None) -> Any:
    if client is not None:
        return client
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RFxDraftError("anthropic package is not installed") from exc
    _require_api_key()
    return anthropic.Anthropic(max_retries=2, timeout=180)


def _tool_def() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Emit the complete structured RFx draft for corrugated packaging.",
        "input_schema": _DraftPayload.model_json_schema(),
    }


def _user_content(
    brief: str,
    *,
    target_lines: int = 30,
    retry_hint: str | None = None,
    context_note: str | None = None,
) -> str:
    text = (
        "Buyer brief (plain language):\n\n"
        f"{brief.strip()}\n\n"
        f"Draft the complete RFx now via emit_rfx. Remember: exactly {target_lines} line items "
        f"(the brief asked for this count — do NOT emit 30 unless N={target_lines}). Title and scope MUST state the same count (~{target_lines} SKUs / {target_lines} line items) — never write a different SKU count in scope than in line_items. "
        "exactly 8 questionnaire items (Q1–Q8 topics as in system prompt) with 3–4 knockout, and exactly 5 vendors."
    )
    if context_note:
        text += f"\n\n{context_note}"
    if retry_hint:
        text += f"\n\n{retry_hint}"
    return text


def _call_haiku(
    *,
    client: Any,
    brief: str,
    target_lines: int = 30,
    retry_hint: str | None = None,
    context_note: str | None = None,
    max_tokens: int = 16000,
) -> _DraftPayload:
    """One forced tool-use Messages API call; validate tool input as _DraftPayload.

    Internal schema-validation retry (up to 2 attempts) is separate from the
    outer line-count retry in draft_rfx / regenerate_line_items.
    """
    messages = [
        {
            "role": "user",
            "content": _user_content(
                brief,
                target_lines=target_lines,
                retry_hint=retry_hint,
                context_note=context_note,
            ),
        }
    ]
    last_error: str | None = None

    for _attempt in range(2):
        resp = client.messages.create(
            model=_haiku_model(),
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=[_tool_def()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=messages,
        )
        tool_use = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_use is None:
            last_error = (
                f"model returned no tool call "
                f"(stop_reason={getattr(resp, 'stop_reason', None)})"
            )
        else:
            try:
                return _DraftPayload.model_validate(tool_use.input)
            except ValidationError as exc:
                last_error = str(exc)[:3000]

        feedback = (
            "Your previous emit_rfx output did not validate. Fix these errors and "
            f"emit again:\n{last_error}"
        )
        messages.append({"role": "assistant", "content": resp.content})
        if tool_use is not None:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": feedback,
                            "is_error": True,
                        }
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": feedback})

    raise RFxDraftError(
        f"Structured RFx draft failed schema validation after retry: {last_error}"
    )


def _new_rfx_id(brief: str) -> str:
    digest = hashlib.sha1(brief.strip().encode("utf-8")).hexdigest()[:6].upper()
    return f"RFX-{date.today().strftime('%Y%m%d')}-{digest}"


def _coerce_vendors(raw: Any) -> list[Vendor]:
    """Validate vendors from model/caller; fall back / pad to exactly 5 seeds."""
    vendors: list[Vendor] = []
    for v in raw or []:
        try:
            if isinstance(v, Vendor):
                vendors.append(v)
            else:
                vendors.append(
                    Vendor.model_validate(
                        v.model_dump() if hasattr(v, "model_dump") else v
                    )
                )
        except (ValidationError, TypeError, ValueError):
            continue

    if len(vendors) < 5:
        seen = {v.vendor_id for v in vendors}
        for seed in _SEED_VENDORS:
            if len(vendors) >= 5:
                break
            if seed.vendor_id not in seen:
                vendors.append(seed)
                seen.add(seed.vendor_id)

    return vendors[:5]


def _coerce_line_items(raw: list[Any]) -> list[LineItem]:
    lines: list[LineItem] = []
    for i, li in enumerate(raw or [], start=1):
        if isinstance(li, LineItem):
            item = li
        else:
            data = li.model_dump() if hasattr(li, "model_dump") else dict(li)
            data.setdefault("line_id", f"L{i:02d}")
            data.setdefault("uom", "piece")
            data.setdefault("specs", {})
            item = LineItem.model_validate(data)
        if not item.line_id:
            item = item.model_copy(update={"line_id": f"L{i:02d}"})
        lines.append(_enrich_specs(item))
    return lines


def _coerce_questionnaire(raw: list[Any]) -> list[QuestionnaireItem]:
    questions: list[QuestionnaireItem] = []
    for i, q in enumerate(raw or [], start=1):
        if isinstance(q, QuestionnaireItem):
            questions.append(q)
        else:
            data = q.model_dump() if hasattr(q, "model_dump") else dict(q)
            data.setdefault("id", f"Q{i}")
            questions.append(QuestionnaireItem.model_validate(data))
    return questions


def _enrich_specs(line: LineItem) -> LineItem:
    """Optionally attach nominal_weight_g when L/W/H/GSM are present in specs."""
    specs = dict(line.specs or {})
    try:
        l = int(specs["length_mm"])
        w = int(specs["width_mm"])
        h = int(specs["height_mm"])
        gsm = int(specs["gsm"])
    except (KeyError, TypeError, ValueError):
        return line
    if "nominal_weight_g" not in specs:
        specs["nominal_weight_g"] = nominal_weight_g(l, w, h, gsm)
    return line.model_copy(update={"specs": specs})


def _ensure_line_count(
    payload: _DraftPayload,
    *,
    client: Any,
    brief: str,
    max_tokens: int,
    target_lines: int,
) -> _DraftPayload:
    """Retry once if line count is not exactly target_lines; then trim/pad softly."""
    n = int(target_lines)
    if len(payload.line_items) == n:
        return payload
    hint = (
        f"Your previous draft had {len(payload.line_items)} line items. "
        f"Produce exactly {n} line items (not 30 unless N={n}), keeping the same style "
        "and following the brief."
    )
    payload = _call_haiku(
        client=client,
        brief=brief,
        target_lines=n,
        retry_hint=hint,
        max_tokens=max_tokens,
    )
    lines = list(payload.line_items)
    if len(lines) > n:
        payload = payload.model_copy(update={"line_items": lines[:n]})
    elif len(lines) < n:
        # One more soft accept: keep what we have only if within 20%; else error
        if len(lines) == 0:
            raise RFxDraftError(
                f"RFx draft still has 0 line items after retry; exactly {n} are required."
            )
        # Pad by cloning last line with new ids rather than failing the buyer
        while len(lines) < n:
            base = lines[len(lines) % max(1, len(lines))]
            idx = len(lines) + 1
            clone = base.model_copy(
                update={
                    "line_id": f"L{idx:02d}",
                    "description": f"{base.description} (variant {idx})",
                }
            )
            lines.append(clone)
        payload = payload.model_copy(update={"line_items": lines})
    return payload


def _require_knockouts(questionnaire: list[Any], *, context: str = "RFx draft") -> None:
    if not questionnaire:
        raise RFxDraftError(f"{context} questionnaire is empty.")
    knockouts = sum(
        1
        for q in questionnaire
        if (q.knockout if hasattr(q, "knockout") else bool(q.get("knockout")))
    )
    if knockouts < 1:
        raise RFxDraftError(
            f"{context} questionnaire must include at least one knockout question."
        )


def _to_rfx(payload: _DraftPayload, brief: str, rfx_id: Optional[str] = None) -> RFx:
    lines = _coerce_line_items(payload.line_items)
    questions = _coerce_questionnaire(payload.questionnaire)
    return RFx(
        rfx_id=rfx_id or _new_rfx_id(brief),
        title=payload.title.strip() or "Corrugated packaging RFx",
        scope=payload.scope.strip(),
        terms=payload.terms.strip(),
        currency=(payload.currency or "INR").strip() or "INR",
        line_items=lines,
        questionnaire=questions,
        vendors=_coerce_vendors(payload.vendors),
    )


def _overlay_edits(rfx: RFx, edits: dict[str, Any]) -> RFx:
    """Apply caller-supplied field overlays; empty dict is a no-op."""
    update: dict[str, Any] = {}
    if "title" in edits and edits["title"] is not None:
        update["title"] = str(edits["title"]).strip()
    if "scope" in edits and edits["scope"] is not None:
        update["scope"] = str(edits["scope"]).strip()
    if "terms" in edits and edits["terms"] is not None:
        update["terms"] = str(edits["terms"]).strip()
    if "currency" in edits and edits["currency"] is not None:
        cur = str(edits["currency"]).strip() or "INR"
        update["currency"] = cur
    if "vendors" in edits and edits["vendors"] is not None:
        update["vendors"] = _coerce_vendors(edits["vendors"])
    if "line_items" in edits and edits["line_items"] is not None:
        update["line_items"] = _coerce_line_items(edits["line_items"])
    if "questionnaire" in edits and edits["questionnaire"] is not None:
        update["questionnaire"] = _coerce_questionnaire(edits["questionnaire"])
    if "rfx_id" in edits and edits["rfx_id"] is not None:
        update["rfx_id"] = str(edits["rfx_id"]).strip()
    if not update:
        return rfx
    return rfx.model_copy(update=update)


def apply_rfx_edits(rfx: RFx, **edits: Any) -> RFx:
    """Pure helper: overlay title/scope/terms/currency/vendors (and optionally
    replace line_items/questionnaire) without calling the LLM.

    Validates the result as an RFx model. Unknown kwargs are ignored.
    """
    if not isinstance(rfx, RFx):
        rfx = RFx.model_validate(rfx)
    updated = _overlay_edits(rfx, edits)
    # Re-validate through the model constructor for a clean copy
    return RFx.model_validate(updated.model_dump())


def draft_rfx(brief: str, **kwargs: Any) -> RFx:
    """Draft a structured RFx from a free-text buyer brief.

    Parameters
    ----------
    brief:
        Plain-language buyer brief (min ~10 characters).
    **kwargs:
        ``client`` — Anthropic-compatible client (inject mock in tests).
        ``rfx_id`` — override generated id.
        ``max_tokens`` — Messages API max_tokens (default 16000).
        ``title``, ``scope``, ``terms``, ``currency``, ``vendors`` —
        manual edits overlaid after the model draft so user edits win.

    Returns
    -------
    RFx
        Validated shared_models.RFx; line-item count follows the brief (default 30).
    """
    brief = (brief or "").strip()
    if len(brief) < 10:
        raise ValueError("Brief is too short — describe category, volume, and site.")

    client = _make_client(kwargs.get("client"))
    max_tokens = int(kwargs.get("max_tokens") or 16000)
    rfx_id = kwargs.get("rfx_id")
    if kwargs.get("target_lines") is not None:
        target_lines = max(1, min(60, int(kwargs["target_lines"])))
    else:
        target_lines = infer_line_count(brief)

    payload = _call_haiku(
        client=client, brief=brief, target_lines=target_lines, max_tokens=max_tokens
    )
    payload = _ensure_line_count(
        payload,
        client=client,
        brief=brief,
        max_tokens=max_tokens,
        target_lines=target_lines,
    )

    if not (8 <= len(payload.questionnaire) <= 10):
        if not payload.questionnaire:
            raise RFxDraftError("RFx draft questionnaire is empty.")

    _require_knockouts(payload.questionnaire)

    rfx = _to_rfx(payload, brief, rfx_id=rfx_id)
    rfx = _align_rfx_prose(rfx, target_lines, brief)

    # Manual edits win over model output for the overlay fields.
    overlay_keys = ("title", "scope", "terms", "currency", "vendors")
    overlays = {k: kwargs[k] for k in overlay_keys if k in kwargs}
    if overlays:
        rfx = _overlay_edits(rfx, overlays)
        # Re-align only fields the caller did not explicitly override.
        remain = tuple(k for k in ("title", "scope", "terms") if k not in overlays)
        if remain:
            rfx = _align_rfx_prose(rfx, len(rfx.line_items), brief, fields=remain)

    return RFx.model_validate(rfx.model_dump())


def regenerate_line_items(
    rfx: RFx,
    brief: str | None = None,
    **kwargs: Any,
) -> RFx:
    """Regenerate ONLY line items from brief + existing RFx context.

    Preserved fields (always win over model output)
    -----------------------------------------------
    ``rfx_id``, ``title``, ``scope``, ``terms``, ``currency`` are taken from
    the existing ``rfx`` and never overwritten by the model. The shared
    ``emit_rfx`` tool still returns a full draft; those fields are discarded.

    Optional refresh flags
    ----------------------
    ``refresh_questionnaire`` (default False) — replace questionnaire from model;
    requires ≥1 knockout when True. Otherwise existing questionnaire is kept.
    ``refresh_vendors`` (default False) — replace vendors from model (seed/pad
    to 5). If False and existing vendors are empty, seed 5.

    Other kwargs: ``client=``, ``max_tokens=``.
    """
    if not isinstance(rfx, RFx):
        rfx = RFx.model_validate(rfx)

    brief_text = (brief or "").strip()
    if not brief_text:
        # Synthesize a regeneration brief from existing RFx context
        brief_text = (
            f"Regenerate line items for RFx '{rfx.title}'.\n"
            f"Scope:\n{rfx.scope}\n"
            f"Terms:\n{rfx.terms}\n"
            f"Currency: {rfx.currency}"
        )
    if len(brief_text) < 10:
        raise ValueError("Brief is too short — describe category, volume, and site.")

    client = _make_client(kwargs.get("client"))
    max_tokens = int(kwargs.get("max_tokens") or 16000)
    refresh_questionnaire = bool(kwargs.get("refresh_questionnaire", False))
    refresh_vendors = bool(kwargs.get("refresh_vendors", False))

    if kwargs.get("target_lines") is not None:
        target_lines = max(1, min(60, int(kwargs["target_lines"])))
    else:
        target_lines = infer_line_count(brief_text, default=len(rfx.line_items) or 30)

    context_note = (
        f"REGENERATION MODE: Produce a fresh set of exactly {target_lines} line items that fit "
        "this existing RFx. Keep title/scope/terms/currency consistent with the "
        "context below (the caller will preserve the existing values regardless).\n"
        f"Existing title: {rfx.title}\n"
        f"Existing scope:\n{rfx.scope}\n"
        f"Existing terms:\n{rfx.terms}\n"
        f"Currency: {rfx.currency}\n"
        f"rfx_id: {rfx.rfx_id}"
    )

    payload = _call_haiku(
        client=client,
        brief=brief_text,
        target_lines=target_lines,
        context_note=context_note,
        max_tokens=max_tokens,
    )
    payload = _ensure_line_count(
        payload,
        client=client,
        brief=brief_text,
        max_tokens=max_tokens,
        target_lines=target_lines,
    )

    new_lines = _coerce_line_items(payload.line_items)

    if refresh_questionnaire:
        new_questions = _coerce_questionnaire(payload.questionnaire)
        _require_knockouts(new_questions, context="Regenerated")
    else:
        new_questions = list(rfx.questionnaire)

    if refresh_vendors:
        new_vendors = _coerce_vendors(payload.vendors)
    else:
        new_vendors = list(rfx.vendors) if rfx.vendors else _coerce_vendors([])

    return rfx.model_copy(
        update={
            "line_items": new_lines,
            "questionnaire": new_questions,
            "vendors": new_vendors,
        }
    )


__all__ = [
    "draft_rfx",
    "regenerate_line_items",
    "apply_rfx_edits",
    "nominal_weight_g",
    "RFxDraftError",
    "DEFAULT_HAIKU_MODEL",
    "infer_line_count",
    "_align_rfx_prose",
]

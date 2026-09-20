"""Agent 1 — RFx Drafter.

Conversational AI co-pilot: turns a plain-language buyer brief into a
validated RFx (scope/title, 30 line items, questionnaire, vendors, terms)
using Anthropic Claude Haiku with forced tool-use for structured output.

Keeps LLM calls local to this module so Sonnet callers in core/llm.py stay
untouched. Importable without starting FastAPI.
"""
from __future__ import annotations

import hashlib
import os
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from shared_models import LineItem, QuestionnaireItem, RFx, Vendor

DEFAULT_HAIKU_MODEL = "claude-3-haiku-20240307"
TOOL_NAME = "emit_rfx"

# Realistic India corrugated shortlist — used only when the model omits vendors
# or returns a list that cannot be coerced to Vendor. Always ends at length 5.
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
- Emit exactly 30 line items via the emit_rfx tool. Mix 3-ply and 5-ply RSC /
  die-cut cartons (at most two 7-ply only if the brief warrants). Dimensions in
  mm, board GSM, flute (B/C/BC/E), print spec, and annual volumes in pieces.
- Put engineering detail in each line's specs dict (keys such as length_mm,
  width_mm, height_mm, ply, gsm, flute, print). Descriptions must be specific
  enough for a vendor to quote without calling back.
- Questionnaire: 8–12 questions on quality systems, BCT/ECT/burst testing,
  material sourcing (FSC / recycled content), food-contact or moisture if
  relevant, capacity, and references. Mark exactly 3 or 4 as knockout=true
  (a 'No' disqualifies).
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
    scope: str = Field(description="Background, what is sourced, volumes, quality, submission instructions")
    terms: str = Field(description="Commercial terms as a single prose block")
    currency: str = "INR"
    line_items: list[_DraftLine] = Field(description="Exactly 30 corrugated line items")
    questionnaire: list[_DraftQuestion] = Field(description="8–12 questions; 3–4 knockout")
    vendors: list[_DraftVendor] = Field(default_factory=list, description="Exactly 5 target vendors")


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


def _user_content(brief: str, retry_hint: str | None = None) -> str:
    text = (
        "Buyer brief (plain language):\n\n"
        f"{brief.strip()}\n\n"
        "Draft the complete RFx now via emit_rfx. Remember: exactly 30 line items, "
        "8–12 questionnaire items with 3–4 knockout, and exactly 5 vendors."
    )
    if retry_hint:
        text += f"\n\n{retry_hint}"
    return text


def _call_haiku(
    *,
    client: Any,
    brief: str,
    retry_hint: str | None = None,
    max_tokens: int = 16000,
) -> _DraftPayload:
    """One forced tool-use Messages API call; validate tool input as _DraftPayload."""
    messages = [{"role": "user", "content": _user_content(brief, retry_hint)}]
    last_error: str | None = None

    for attempt in range(2):
        resp = client.messages.create(
            model=_haiku_model(),
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=[_tool_def()],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=messages,
        )
        tool_use = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if tool_use is None:
            last_error = f"model returned no tool call (stop_reason={getattr(resp, 'stop_reason', None)})"
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

    raise RFxDraftError(f"Structured RFx draft failed schema validation after retry: {last_error}")


def _new_rfx_id(brief: str) -> str:
    digest = hashlib.sha1(brief.strip().encode("utf-8")).hexdigest()[:6].upper()
    return f"RFX-{date.today().strftime('%Y%m%d')}-{digest}"


def _coerce_vendors(raw: list[_DraftVendor] | list[Vendor]) -> list[Vendor]:
    """Validate model vendors; fall back / pad to exactly 5 seed vendors."""
    vendors: list[Vendor] = []
    for v in raw or []:
        try:
            if isinstance(v, Vendor):
                vendors.append(v)
            else:
                vendors.append(Vendor.model_validate(v.model_dump() if hasattr(v, "model_dump") else v))
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


def _to_rfx(payload: _DraftPayload, brief: str, rfx_id: Optional[str] = None) -> RFx:
    lines = [
        _enrich_specs(
            LineItem(
                line_id=li.line_id or f"L{i:02d}",
                description=li.description,
                qty=float(li.qty),
                uom=li.uom or "piece",
                specs=dict(li.specs or {}),
            )
        )
        for i, li in enumerate(payload.line_items, start=1)
    ]
    # Enforce sequential ids if the model drifted
    for i, li in enumerate(lines, start=1):
        if not li.line_id:
            lines[i - 1] = li.model_copy(update={"line_id": f"L{i:02d}"})

    questions = [
        QuestionnaireItem(
            id=q.id or f"Q{i}",
            question=q.question,
            knockout=bool(q.knockout),
        )
        for i, q in enumerate(payload.questionnaire, start=1)
    ]

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


def draft_rfx(brief: str, **kwargs) -> RFx:
    """Draft a structured RFx from a free-text buyer brief.

    Parameters
    ----------
    brief:
        Plain-language buyer brief.
    **kwargs:
        Optional: ``client`` (Anthropic-compatible client, useful in tests),
        ``rfx_id`` (override id), ``max_tokens``.

    Returns
    -------
    RFx
        Validated shared_models.RFx with exactly 30 line items and 5 vendors.
    """
    brief = (brief or "").strip()
    if len(brief) < 10:
        raise ValueError("Brief is too short — describe category, volume, and site.")

    client = _make_client(kwargs.get("client"))
    max_tokens = int(kwargs.get("max_tokens") or 16000)
    rfx_id = kwargs.get("rfx_id")

    payload = _call_haiku(client=client, brief=brief, max_tokens=max_tokens)

    if len(payload.line_items) != 30:
        hint = (
            f"Your previous draft had {len(payload.line_items)} line items. "
            "Produce exactly 30 line items, keeping the same style and following the brief."
        )
        payload = _call_haiku(
            client=client, brief=brief, retry_hint=hint, max_tokens=max_tokens
        )
        if len(payload.line_items) != 30:
            raise RFxDraftError(
                f"RFx draft still has {len(payload.line_items)} line items after one retry; "
                "exactly 30 are required."
            )

    if not (8 <= len(payload.questionnaire) <= 12):
        # Soft: keep model output if close; hard-fail only on empty
        if not payload.questionnaire:
            raise RFxDraftError("RFx draft questionnaire is empty.")

    knockouts = sum(1 for q in payload.questionnaire if q.knockout)
    if knockouts < 1:
        raise RFxDraftError(
            "RFx draft questionnaire must include at least one knockout question."
        )

    return _to_rfx(payload, brief, rfx_id=rfx_id)


# Quiet unused-import guard for public re-exports used by tests / callers
__all__ = [
    "draft_rfx",
    "nominal_weight_g",
    "RFxDraftError",
    "DEFAULT_HAIKU_MODEL",
]

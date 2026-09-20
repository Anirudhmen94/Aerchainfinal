"""Turn a plain-language brief into a complete RFx (AI), then enrich it with
deterministic engineering data (nominal carton weight) that later powers
per-kg to per-piece conversion."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import llm
from .models import RFxDraftAI

SYSTEM = """You are a senior category manager for packaging procurement in India, drafting an RFx
(request for quotation) for corrugated packaging on behalf of a buyer.

Produce a realistic, procurement-grade RFx that a supplier would recognise:
- EXACTLY 30 line items. Mix 3-ply and 5-ply (and at most two 7-ply if the brief warrants).
  Use realistic RSC / die-cut carton dimensions in mm, typical Indian board grammages
  (3-ply ~ 350-500 gsm, 5-ply ~ 600-900 gsm, 7-ply ~ 1000-1300 gsm), flute profiles (B, C, BC, E),
  and print specs. Quantities should be annual volumes appropriate to the brief. SKU codes sequential.
- Descriptions must be specific enough that a vendor could quote them without calling back.
- Commercial terms: INR per piece, delivered, exclusive of GST is the default basis unless the brief says otherwise.
- Questionnaire: 8-12 questions on quality systems, testing (BCT/ECT/bursting), material sourcing
  (FSC / recycled content), food-contact or moisture requirements if relevant, capacity, and
  references. Mark 3-4 as knockout (a 'No' disqualifies).
- Follow the brief's numbers if it gives any (volumes, sizes, plant location, timelines). Never contradict it.
- Do not invent supplier names or prices. This is the buyer's document.
"""


def nominal_weight_g(length_mm: int, width_mm: int, height_mm: int, gsm: int) -> float:
    """Approximate blank weight of a regular slotted carton (RSC).

    Blank length = 2*(L+W) + 40 mm glue flap; blank height = H + W (two flaps of W/2).
    weight = area(m2) * gsm. This is a nominal figure used only to convert per-kg quotes
    to per-piece for comparison, and is always disclosed alongside the conversion.
    """
    blank_len = 2 * (length_mm + width_mm) + 40
    blank_h = height_mm + width_mm
    area_m2 = (blank_len * blank_h) / 1_000_000
    return round(area_m2 * gsm, 1)


def draft_rfx(brief: str, log: list | None = None) -> dict:
    content = (
        "Buyer brief (plain language):\n\n" + brief.strip() + "\n\n"
        "Draft the complete RFx now. Remember: exactly 30 line items."
    )
    draft = llm.structured(purpose="draft_rfx", system=SYSTEM, content=content, schema=RFxDraftAI, max_tokens=16000, log=log)

    if len(draft.line_items) != 30:
        content2 = (
            content
            + f"\n\nYour previous draft had {len(draft.line_items)} line items. Produce exactly 30, keeping the same style."
        )
        draft = llm.structured(purpose="draft_rfx_retry", system=SYSTEM, content=content2, schema=RFxDraftAI, max_tokens=16000, log=log)

    data = draft.model_dump(mode="json")
    for i, li in enumerate(data["line_items"], start=1):
        li["line_no"] = i  # enforce sequential numbering regardless of model output
        li["uom"] = "pcs"
        li["nominal_weight_g"] = nominal_weight_g(li["length_mm"], li["width_mm"], li["height_mm"], li["gsm"])
        li["dimensions"] = f'{li["length_mm"]}x{li["width_mm"]}x{li["height_mm"]} mm'
    for i, q in enumerate(data["questionnaire"], start=1):
        q["q_id"] = f"Q{i}"
    data["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return data


def new_state(brief: str, rfx: dict) -> dict:
    return {
        "id": uuid.uuid4().hex[:10],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "brief": brief,
        "status": "draft",
        "rfx": rfx,
        "outbox": [],
        "vendors": [],
        "fx": {"as_of": "2026-09-15", "source": "Buyer treasury reference rate (fixed for this event)", "rates_to_inr": {"INR": 1.0, "USD": 83.50, "EUR": 91.20, "GBP": 106.40, "AED": 22.73, "SGD": 62.10}},
        "reviews": [],
        "chat": [],
        "recommendation": None,
        "ai_log": [],
    }

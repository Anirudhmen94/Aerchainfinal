"""AI-drafted clarification emails for a vendor's unresolved / needs-review items. Sending is stubbed."""
from __future__ import annotations

from . import engine, llm
from .models import ClarificationEmailAI

SYSTEM = """You draft short, courteous, specific clarification emails from a buyer's sourcing team to a supplier.
List each open point as a numbered question the supplier can answer in one line. Do not suggest prices or answers.
Do not invent facts about the supplier's offer beyond the open points given. Sign off as 'Category Sourcing'."""


def open_points(state: dict, vendor_id: str) -> list[str]:
    cmp = engine.build_comparison(state)
    v = next(v for v in cmp["vendors"] if v["vendor_id"] == vendor_id)
    pts = []
    for ln in cmp["lines"]:
        c = ln["cells"][vendor_id]
        if c["status"] in ("needs_review", "unresolved"):
            pts.append(f"Line {ln['line_no']} ({ln['sku']} {ln['description']}): status {c['status']}. {c.get('reason','')} {('Quoted: ' + str(c.get('raw_price')) + ' ' + str(c.get('currency') or '') + ' ' + str(c.get('basis') or '')) if c.get('raw_price') is not None else ''}".strip())
        elif c["status"] == "missing":
            pts.append(f"Line {ln['line_no']} ({ln['sku']} {ln['description']}): no price found in your response.")
    q = v["questionnaire"]
    for qid in q["knockout_open"]:
        row = next(r for r in q["rows"] if r["q_id"] == qid)
        pts.append(f"Questionnaire {qid} (knockout): {row['text']} — {'no answer found' if not row['answered'] else 'answer unclear: ' + row['answer']}")
    for c in v["certificates"]:
        if c.get("claimed_in_text") and not c.get("attached_as_document"):
            pts.append(f"Certificate {c['name']}: mentioned but not attached; please send a copy.")
    if v["commercial"].get("freight_extra"):
        pts.append("Freight: your quote excludes freight; please provide a delivered price or a freight rate per trip/tonne to our plant.")
    return pts


def draft_clarification(state: dict, vendor: dict, log: list | None = None) -> dict:
    pts = open_points(state, vendor["vendor_id"])
    if not pts:
        return {"subject": f"Re: RFQ {state['rfx']['title']} — no clarifications needed", "body": "All lines and questionnaire items are resolved. No email drafted.", "points": []}
    content = (
        f"Supplier: {vendor['name']}\nRFx: {state['rfx']['title']} (ref {state['id']})\n"
        f"Reply deadline: 3 working days.\n\nOpen points to clarify (one numbered question each):\n- " + "\n- ".join(pts)
    )
    res = llm.structured(purpose="clarification_email", system=SYSTEM, content=content, schema=ClarificationEmailAI, max_tokens=3000, log=log)
    return {"subject": res.subject, "body": res.body, "points": pts}

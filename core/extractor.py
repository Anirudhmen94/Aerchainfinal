"""Read one vendor's reply (all its files) into a structured, evidence-backed
record, then *ground* it: every quoted snippet is checked against the source
text and anything that cannot be found verbatim is downgraded to needs_review.

The model is told what the RFx asked for so it can match vendor rows to RFx
lines. It is not told what prices to expect, and it never computes totals or
conversions: those happen in engine.py.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone

from . import ingest, llm
from .models import ExtractionAI

SYSTEM = """You are a meticulous procurement analyst reading a supplier's response to an RFx for corrugated packaging.
Your job is to report exactly what the supplier's documents say and where they say it. You are not allowed to
guess, fill gaps, or 'helpfully' compute anything.

Rules:
1. EVIDENCE IS MANDATORY. Every price, term, questionnaire answer and certificate you report must carry an
   evidence object whose `snippet` is copied VERBATIM (character for character, 5-200 chars) from the anchored
   text you are given, and whose `location` is the anchor tag on that line (e.g. "Quotation!F14", "page 2",
   "para 6", "table 1 row 4", "image line 12", "line 3"). If you cannot point at text, do not report the value.
   Never abbreviate a snippet with "..." and never shorten a table row: if a passage is long, quote the shortest
   contiguous span that contains the value (e.g. "priced separately at USD 183.5 per thousand").
2. NEVER INVENT. If a line item is not priced anywhere, put it in `not_quoted_line_nos`. If a currency, unit basis
   or quantity basis is not stated, leave it null/unknown and mark status needs_review with the reason.
   If the supplier refers to information you do not have ("same as last year", "as per previous contract",
   "on request"), report the affected lines as status `unresolved` with that snippet as evidence.
3. PRICE BASIS. Record the price exactly as written and classify its basis: per_pc, per_100, per_1000, per_kg,
   per_box, per_bundle. Fill `basis_qty` when the document states how many pieces a box/bundle/lot contains.
   If "per box" could mean one carton or a bundle of cartons and the document does not say, use per_box with
   basis_qty null and status needs_review. Do NOT convert to per-piece yourself.
4. ROW MATCHING. Map each supplier row to an RFx line number using the supplier's own line references first,
   then dimensions, ply and description. If a row could match more than one RFx line, set line_no null, list the
   candidates, and mark needs_review. If the supplier gives one price for a group (e.g. "all 3-ply items",
   "lines 4, 7, 9"), emit one line_quote per covered RFx line, each with the same evidence snippet.
   Per-kg prices stated by board grade apply to every RFx line of that grade; emit one line_quote per such line
   with status ok (the buyer's system will do the weight conversion and flag it).
5. ALTERNATES AND DEVIATIONS. If the supplier offers a different spec than asked (another flute, another GSM),
   still map it to the RFx line but mark needs_review and explain in `reason`.
6. FOOTNOTES AND SMALL PRINT MATTER. Discounts, freight, GST, validity and payment terms are often in footnotes
   or paragraphs. Report each as a commercial term with its evidence. For discounts, fill numeric_pct and applies_to.
7. QUESTIONNAIRE. For every RFx question id, report the supplier's answer if one exists (answered=true) or
   answered=false with an empty answer. A narrative like "we hold ISO 9001" answers a yes/no question with "Yes".
8. CERTIFICATES. attached_as_document=true only if a certificate file itself is among the files listed; a mention
   like "certificate available on request" is claimed_in_text=true, attached_as_document=false.
9. Confidence is your honest estimate (0-1) that the value and its mapping are correct. Photographed or
   transcribed documents deserve lower confidence on digits marked '?' or rows marked '[cut off]'.
10. WHAT STATUS MEANS. A line quote's status is about whether the PRICE VALUE and its LINE MAPPING are reliable.
   - ok: the number is clearly legible, its basis (per pc / per 100 / per 1000 / per kg / per box of N) and currency
     are stated or unambiguous, and the row clearly maps to one RFx line (by the supplier's own line reference, or by
     dimensions + ply). A truncated description is fine when size, ply and sequence agree.
   - Do NOT use needs_review merely because the supplier's basis differs from what the RFx asked for (per 100 instead
     of per piece, USD instead of INR, per kg, per bundle of a stated size, ex-works / CIF instead of delivered,
     freight or GST extra). Record the basis faithfully; report incoterm / freight / GST differences ONCE as commercial
     terms and in `notes`. The buyer's system converts units and currencies and flags those cells itself.
   - needs_review: ambiguous mapping (candidates), illegible or '?' digits, an alternate specification offered,
     pack size or currency not stated, a value you had to infer, or conflicting figures for the same row.
   - unresolved: the price depends on information not in the documents ("same as last year", "on request").
   Always fill `review_cause`. Examples:
     "Rs. 330 per box (bundle) of 20 nos" → price 330, INR, per_bundle, basis_qty 20, status ok, review_cause none.
     "USD 614.5 per 1,000 pcs CIF Nhava Sheva" → price 614.5, USD, per_1000, basis_qty 1000, status ok, cause none
       (report CIF / inland freight excluded as a freight term).
     "Rs. 1693 per 100 Nos" → per_100, basis_qty 100, ok.  "51/kg for the 3-ply" → per_kg, ok, one quote per 3-ply line.
     Row says "400x300x250 3-ply" but two RFx lines share that size (one printed, one plain) and the row does not
       say which → line_no null, candidates [1, 2], needs_review, cause ambiguous_mapping.
     Row omits flute but size + ply + the supplier's own line number match exactly one RFx line → ok, cause none,
       confidence ~0.85; mention the unconfirmed flute in `reason` only if you consider it material (cause spec_not_confirmed).
"""


def _rfx_context(rfx: dict) -> str:
    lines = ["RFx LINE ITEMS (line_no | sku | description | board | flute | L x W x H mm | gsm | print | annual qty pcs):"]
    for li in rfx["line_items"]:
        lines.append(
            f"{li['line_no']} | {li['sku']} | {li['description']} | {li['board']} | {li['flute']} | "
            f"{li['length_mm']}x{li['width_mm']}x{li['height_mm']} | {li['gsm']} | {li['print']} | {li['annual_qty']}"
        )
    lines.append("")
    lines.append(f"RFx PRICE BASIS REQUESTED: {rfx['terms']['price_basis']}; currency {rfx['terms']['currency']}.")
    lines.append("")
    lines.append("QUESTIONNAIRE (q_id | type | knockout | question):")
    for q in rfx["questionnaire"]:
        lines.append(f"{q['q_id']} | {q['answer_type']} | {'knockout' if q['knockout'] else '-'} | {q['text']}")
    return "\n".join(lines)


def ingest_vendor_files(vendor: dict, get_bytes, log: list | None = None) -> None:
    """Populate vendor['texts'][file_id] for files not yet converted."""
    vendor.setdefault("texts", {})
    for f in vendor["files"]:
        if f["file_id"] in vendor["texts"]:
            continue
        data = get_bytes(f["url"])
        if data is None:
            vendor["texts"][f["file_id"]] = {"text": "", "method": "missing file", "error": "file not found in storage"}
            continue
        try:
            vendor["texts"][f["file_id"]] = ingest.file_to_text(f["name"], data, f.get("kind"), log=log)
        except Exception as e:
            vendor["texts"][f["file_id"]] = {"text": "", "method": "failed", "error": str(e)}


def _content_blocks(vendor: dict, rfx: dict, get_bytes) -> list[dict]:
    blocks = [llm.text_block(_rfx_context(rfx))]
    file_list = ", ".join(f"{f['name']} ({f['kind']}, {f.get('role','quote')})" for f in vendor["files"])
    blocks.append(llm.text_block(f"\nSUPPLIER: {vendor['name']} ({vendor['city']}). FILES RECEIVED: {file_list}\n"))
    for f in vendor["files"]:
        t = vendor["texts"].get(f["file_id"], {})
        header = f"\n===== FILE: {f['name']} | kind: {f['kind']} | read via: {t.get('method','?')}"
        if t.get("legibility") is not None:
            header += f" | legibility {t['legibility']:.2f}"
        if t.get("caveats"):
            header += f" | transcriber caveats: {t['caveats']}"
        header += " =====\n"
        blocks.append(llm.text_block(header + (t.get("text") or "(no text could be read)")))
        if f["kind"] == "image":
            data = get_bytes(f["url"])
            if data:
                media = ingest.guess_content_type(f["name"])
                blocks.append(llm.text_block("The photograph itself, for verifying digits in the transcription above:"))
                blocks.append(llm.image_block(data, media if media.startswith("image/") else "image/jpeg"))
    blocks.append(llm.text_block("\nNow emit the structured extraction for this supplier. Every value needs verbatim evidence."))
    return blocks


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

_ANCHOR_RE = re.compile(r"\[[^\]]{1,40}\]")
_SEP_RE = re.compile(r"[|\u00b7;:,]+")


def _canon(text: str) -> str:
    """Anchor tags removed, separators and whitespace collapsed: lets a snippet that spans
    several anchored lines (a table row read across cells) still be matched verbatim."""
    return ingest.normalize_ws(_SEP_RE.sub(" ", _ANCHOR_RE.sub(" ", text or "")))


def _find_snippet(snippet: str, texts: dict[str, str]) -> tuple[bool, str | None, float]:
    """Return (verified, file_id, score). Exact normalized substring first, then fuzzy window match."""
    s = ingest.normalize_ws(snippet)
    if len(s) < 3:
        return False, None, 0.0
    for fid, txt in texts.items():
        if s in ingest.normalize_ws(txt):
            return True, fid, 1.0
    sc = _canon(snippet)
    if len(sc) >= 3:
        for fid, txt in texts.items():
            if sc in _canon(txt):
                return True, fid, 0.99
    # Elided quote ("Line 1 ... is priced separately at USD 183.5"): every kept segment must be present, in order.
    segments = [_canon(p) for p in re.split(r"\.{3}|\u2026", snippet) if len(_canon(p)) >= 3]
    if len(segments) > 1:
        for fid, txt in texts.items():
            ct = _canon(txt)
            pos = 0
            ok = True
            for seg in segments:
                idx = ct.find(seg, pos)
                if idx < 0 or idx - pos > 600:
                    ok = False
                    break
                pos = idx + len(seg)
            if ok:
                return True, fid, 0.95
    # Abbreviated table row: all numeric tokens and nearly all words of the snippet appear on one source line.
    tokens = [t for t in re.findall(r"[a-z0-9][a-z0-9.\-]*", sc) if len(t) > 2 or t.isdigit()]
    numeric = [t for t in tokens if re.search(r"\d", t)]
    if len(tokens) >= 3:
        for fid, txt in texts.items():
            for line in txt.splitlines():
                ln = _canon(line)
                if not ln:
                    continue
                hit = sum(1 for t in tokens if t in ln)
                if hit / len(tokens) >= 0.85 and all(n in ln for n in numeric):
                    return True, fid, round(hit / len(tokens), 2)
    # fuzzy: compare against each line of each file (anchored lines are short)
    best = (0.0, None)
    for fid, txt in texts.items():
        for line in txt.splitlines():
            ln = ingest.normalize_ws(re.sub(r"^\[[^\]]+\]\s*", "", line))
            if not ln:
                continue
            ratio = difflib.SequenceMatcher(None, s, ln).ratio()
            if s in ln or ratio > best[0]:
                best = (max(ratio, 1.0 if s in ln else ratio), fid)
    if best[0] >= 0.88:
        return True, best[1], best[0]
    return False, best[1], best[0]


def ground(extraction: dict, vendor: dict) -> dict:
    texts = {fid: t.get("text", "") for fid, t in vendor.get("texts", {}).items()}
    name_to_id = {f["name"]: f["file_id"] for f in vendor["files"]}
    stats = {"checked": 0, "verified": 0, "downgraded": 0}

    def check(obj: dict, allow_downgrade: bool = True):
        ev = obj.get("evidence")
        if not ev:
            if obj.get("status") == "ok" and allow_downgrade:
                obj["status"] = "needs_review"
                obj["reason"] = (obj.get("reason") or "") + " No evidence snippet was provided."
                stats["downgraded"] += 1
            return
        stats["checked"] += 1
        ok, fid, score = _find_snippet(ev.get("snippet", ""), texts)
        ev["verified"] = ok
        ev["match_score"] = round(score, 2)
        ev["file_id"] = fid or name_to_id.get(ev.get("file_name"))
        if not ok and allow_downgrade:
            stats["downgraded"] += 1
            if obj.get("status") == "ok":
                obj["status"] = "needs_review"
            obj["reason"] = (obj.get("reason") or "").strip() + " Evidence snippet could not be found verbatim in the source file."
        elif ok:
            stats["verified"] += 1

    for q in extraction.get("line_quotes", []):
        # The engine converts units, currencies and pack sizes itself; a basis difference alone is not a review reason.
        if q.get("status") == "needs_review" and q.get("review_cause") == "basis_differs_from_rfx" and q.get("price") is not None and q.get("line_no") is not None:
            if q.get("price_basis") not in ("per_box", "per_bundle") or q.get("basis_qty"):
                q["status"] = "ok"
                q["reason"] = ("Basis differs from RFx; converted by the engine. " + (q.get("reason") or "")).strip()
                stats["promoted_basis_only"] = stats.get("promoted_basis_only", 0) + 1
        check(q)
        if q.get("status") == "ok" and q.get("price") is None:
            q["status"] = "needs_review"
            q["reason"] = (q.get("reason") or "") + " No numeric price."
        if q.get("line_no") is None and q.get("status") == "ok":
            q["status"] = "needs_review"
            q["reason"] = (q.get("reason") or "") + " Row not mapped to an RFx line."
    for t in extraction.get("commercials", []):
        check(t)
    for a in extraction.get("questionnaire", []):
        if a.get("answered"):
            check(a)
    for c in extraction.get("certificates", []):
        check(c, allow_downgrade=False)
    extraction["grounding"] = stats
    return extraction


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def extract_vendor(state: dict, vendor: dict, get_bytes, log: list | None = None) -> dict:
    rfx = state["rfx"]
    ingest_vendor_files(vendor, get_bytes, log=log)
    if not any((t.get("text") or "").strip() for t in vendor["texts"].values()):
        raise ValueError("None of the vendor's files contained readable text.")
    result = llm.structured(
        purpose="extract_vendor_response",
        system=SYSTEM,
        content=_content_blocks(vendor, rfx, get_bytes),
        schema=ExtractionAI,
        max_tokens=16000,
        log=log,
    )
    if log:
        log[-1]["vendor"] = vendor["name"]
    data = result.model_dump(mode="json")
    data = ground(data, vendor)
    data["model"] = llm.model_name()
    data["extracted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    valid_lines = {li["line_no"] for li in rfx["line_items"]}
    quoted = {q["line_no"] for q in data["line_quotes"] if q.get("line_no") in valid_lines}
    covered = quoted | set(data.get("not_quoted_line_nos", []))
    # Lines the model neither quoted nor listed as not quoted are recorded as missing, explicitly.
    data["not_quoted_line_nos"] = sorted((set(data.get("not_quoted_line_nos", [])) | (valid_lines - covered)) - quoted)
    return data

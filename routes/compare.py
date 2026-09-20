"""Comparison grid, evidence drawer, reviews, award page and exports."""
from __future__ import annotations

import io
import re

import pymupdf
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from core import engine, export, ingest, storage
from core.web import error_fragment, hx_redirect, load_or_404, now_iso, render

router = APIRouter()


def _vendor(state: dict, vendor_id: str) -> dict:
    for v in state["vendors"]:
        if v["vendor_id"] == vendor_id:
            return v
    raise HTTPException(404, "vendor not found")


def _context_lines(text: str, snippet: str, radius: int = 3) -> list[dict]:
    """Anchored text lines around the one containing the snippet."""
    if not text:
        return []
    lines = text.splitlines()
    target = ingest.normalize_ws(snippet)
    hit = None
    for i, ln in enumerate(lines):
        if target and target in ingest.normalize_ws(ln):
            hit = i
            break
    if hit is None and target:
        # fall back to the first numeric token of the snippet
        nums = re.findall(r"\d[\d,\.]*", snippet)
        for i, ln in enumerate(lines):
            if nums and nums[0] in ln:
                hit = i
                break
    if hit is None:
        return []
    return [{"text": ln, "hit": i == hit} for i, ln in enumerate(lines) if hit - radius <= i <= hit + radius]


@router.get("/rfx/{rfx_id}/compare", response_class=HTMLResponse)
def compare_page(request: Request, rfx_id: str):
    state = load_or_404(rfx_id)
    cmp = engine.build_comparison(state)
    return render(request, "compare.html", state=state, cmp=cmp, active="compare")


@router.get("/rfx/{rfx_id}/evidence/{vendor_id}/{line_no}", response_class=HTMLResponse)
def evidence(request: Request, rfx_id: str, vendor_id: str, line_no: int):
    state = load_or_404(rfx_id)
    vendor = _vendor(state, vendor_id)
    cmp = engine.build_comparison(state)
    line = next((ln for ln in cmp["lines"] if ln["line_no"] == line_no), None)
    if not line:
        raise HTTPException(404)
    cell = line["cells"][vendor_id]
    ev = cell.get("evidence") or {}
    file = None
    if ev:
        file = next((f for f in vendor["files"] if f["file_id"] == ev.get("file_id")), None) or next((f for f in vendor["files"] if f["name"] == ev.get("file_name")), None)
    text = vendor.get("texts", {}).get(file["file_id"], {}).get("text", "") if file else ""
    ctx = _context_lines(text, ev.get("snippet", "")) if ev else []
    # other vendor rows the model mapped to this line (duplicates / alternates)
    others = [q for q in (vendor.get("extraction") or {}).get("line_quotes", []) if q.get("line_no") == line_no]
    return render(request, "partials/evidence_drawer.html", state=state, v=vendor, line=line, cell=cell, ev=ev, file=file, ctx=ctx, others=others, fx=state["fx"])


@router.get("/rfx/{rfx_id}/evidence-image/{vendor_id}/{file_id}")
def evidence_image(rfx_id: str, vendor_id: str, file_id: str, q: str = "", page: int = 0):
    """Render the PDF page containing the snippet with the match highlighted."""
    state = load_or_404(rfx_id)
    vendor = _vendor(state, vendor_id)
    f = next((f for f in vendor["files"] if f["file_id"] == file_id), None)
    if not f or f["kind"] != "pdf":
        raise HTTPException(404)
    data = storage.get_bytes(f["url"])
    if not data:
        raise HTTPException(404)
    doc = pymupdf.open(stream=data, filetype="pdf")
    needles = [q.strip()]
    if len(q) > 40:
        needles += [q[:40].strip(), q[-40:].strip()]
    nums = re.findall(r"\d[\d,\.]*\d", q)
    needles += nums[:2]
    target_page, rects = None, []
    for pg in doc:
        for n in needles:
            if not n:
                continue
            r = pg.search_for(n)
            if r:
                target_page, rects = pg, r
                break
        if target_page is not None:
            break
    if target_page is None:
        target_page = doc[min(page, len(doc) - 1)]
    for r in rects:
        target_page.draw_rect(r + (-2, -2, 2, 2), color=(1, 0.6, 0), width=1.5)
        target_page.draw_rect(r + (-2, -2, 2, 2), color=None, fill=(1, 0.85, 0.2), fill_opacity=0.35)
    pix = target_page.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7))
    png = pix.tobytes("png")
    doc.close()
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/rfx/{rfx_id}/review/{vendor_id}/{line_no}", response_class=HTMLResponse)
def review(request: Request, rfx_id: str, vendor_id: str, line_no: int, action: str = Form(...), value_inr: str = Form(""), note: str = Form("")):
    state = load_or_404(rfx_id)
    vendor = _vendor(state, vendor_id)
    if action not in ("accept", "override", "clear"):
        return error_fragment("Unknown action", 400)
    state["reviews"] = [r for r in state.get("reviews", []) if not (r["vendor_id"] == vendor_id and r["line_no"] == line_no)]
    if action != "clear":
        val = None
        if action == "accept":
            cmp = engine.build_comparison({**state, "reviews": []})
            cell = next(ln for ln in cmp["lines"] if ln["line_no"] == line_no)["cells"].get(vendor_id, {})
            if cell.get("unit_inr") is None:
                return error_fragment("This cell has no per-piece price to accept. Use 'Override' with the confirmed INR value, or leave it excluded.", 400)
        if action == "override":
            try:
                val = float(value_inr.replace(",", ""))
            except ValueError:
                return error_fragment("Enter a numeric INR per-piece value to override.", 400)
        if not note.strip():
            return error_fragment("A note is required so the audit log explains the decision.", 400)
        state["reviews"].append({"vendor_id": vendor_id, "vendor_name": vendor["name"], "line_no": line_no, "action": action, "value_inr": val, "note": note.strip(), "at": now_iso()})
    storage.save_state(rfx_id, state)
    resp = HTMLResponse("")
    resp.headers["HX-Refresh"] = "true"
    return resp


@router.get("/rfx/{rfx_id}/vendor/{vendor_id}/questionnaire", response_class=HTMLResponse)
def questionnaire(request: Request, rfx_id: str, vendor_id: str):
    state = load_or_404(rfx_id)
    vendor = _vendor(state, vendor_id)
    cmp = engine.build_comparison(state)
    vrow = next(v for v in cmp["vendors"] if v["vendor_id"] == vendor_id)
    return render(request, "partials/vendor_drawer.html", state=state, v=vendor, vrow=vrow)


@router.get("/rfx/{rfx_id}/award", response_class=HTMLResponse)
def award_page(request: Request, rfx_id: str):
    state = load_or_404(rfx_id)
    cmp = engine.build_comparison(state)
    split = engine.cheapest_per_line(cmp, None, False, False) if any(v.get("extraction") for v in state["vendors"]) else None
    return render(request, "award.html", state=state, cmp=cmp, split=split, active="award")


@router.get("/rfx/{rfx_id}/export.xlsx")
def export_xlsx(rfx_id: str):
    state = load_or_404(rfx_id)
    data = export.award_workbook(state)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="award-{rfx_id}.xlsx"'})


@router.get("/rfx/{rfx_id}/export.csv")
def export_csv(rfx_id: str):
    state = load_or_404(rfx_id)
    return Response(export.comparison_csv(state), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="comparison-{rfx_id}.csv"'})


@router.get("/rfx/{rfx_id}/memo.md")
def export_memo(rfx_id: str):
    state = load_or_404(rfx_id)
    return Response(export.award_memo_md(state), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="award-memo-{rfx_id}.md"'})

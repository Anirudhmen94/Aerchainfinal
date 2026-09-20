"""Aerchain RFx Crew — free tabbed workspace (Draft | Send | Inbox | Compare | Ask | Award).

Local:  uvicorn app_crew:app --port 8518 --reload
Vercel: module-level `app` (see vercel.json).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from orchestrator.pipeline import (  # noqa: E402
    WIZARD_STEPS,
    RFxPipeline,
    STORE_DIR,
    VENDOR_DIR,
    load_pipeline,
)

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="Aerchain RFx Crew", version="0.3.0")

_SESSIONS: dict[str, RFxPipeline] = {}


def _session(rfx_id: Optional[str] = None) -> RFxPipeline:
    if rfx_id and rfx_id in _SESSIONS:
        return _SESSIONS[rfx_id]
    if rfx_id:
        pipe = load_pipeline(rfx_id)
        if pipe.rfx:
            _SESSIONS[rfx_id] = pipe
            return pipe
    return RFxPipeline()


def _save(pipe: RFxPipeline) -> None:
    if pipe.rfx:
        _SESSIONS[pipe.rfx.rfx_id] = pipe
        pipe._persist()


def _render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _wizard_ctx(pipe: RFxPipeline) -> dict[str, Any]:
    step = pipe.wizard_step if pipe.wizard_step in WIZARD_STEPS else "draft"
    unlocked = pipe.unlocked_steps()  # always all tabs
    done = pipe.completion()
    # Per-line eligible vendors for Award tab
    eligible_by_line: dict[str, list[dict[str, Any]]] = {}
    if pipe.rfx and pipe.comparison:
        for li in pipe.rfx.line_items:
            eligible_by_line[li.line_id] = pipe.eligible_vendors_for_line(li.line_id)
    # Matrix helpers
    cell_map: dict[tuple[str, str], Any] = {}
    vendors_in_matrix: list[str] = []
    if pipe.comparison:
        seen: list[str] = []
        for c in pipe.comparison.cells:
            cell_map[(c.line_id, c.vendor_id)] = c
            if c.vendor_id not in seen:
                seen.append(c.vendor_id)
        vendors_in_matrix = seen
    qual_map = {}
    if pipe.comparison:
        for q in pipe.comparison.qualifications or []:
            qual_map[q.vendor_id] = q
    # Evidence snippets per vendor (from parsed quotes)
    evidence_by_vendor: dict[str, str] = {}
    for q in pipe.quotes or []:
        bits = []
        for item in (q.raw_evidence or []):
            if isinstance(item, dict):
                sn = str(item.get("snippet") or "").strip()
                if sn and item.get("kind") != "meta":
                    bits.append(sn)
        if q.notes:
            bits.append(str(q.notes))
        if bits:
            evidence_by_vendor[q.vendor_id] = " | ".join(bits)[:400]

    suggested_questions = [
        "Cheapest per line among qualified vendors?",
        "Where are the gaps and uncertain cells?",
        "Which vendors failed knockouts and why?",
        "Give an award recommendation I can defend to a VP.",
        "Show me USD / UOM conversions that changed the matrix.",
    ]

    return {
        "pipe": pipe,
        "rfx": pipe.rfx,
        "step": step,
        "steps": WIZARD_STEPS,
        "unlocked": unlocked,
        "done": done,
        "eligible_by_line": eligible_by_line,
        "cell_map": cell_map,
        "vendors_in_matrix": vendors_in_matrix,
        "qual_map": qual_map,
        "award_summary": pipe.award_summary() if pipe.awards or pipe.award_validation else None,
        "snapshot": pipe.snapshot(),
        "evidence_by_vendor": evidence_by_vendor,
        "suggested_questions": suggested_questions,
    }


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "app": "rfx-crew",
        "tabs": WIZARD_STEPS,
        "wizard": WIZARD_STEPS,
        "agents": [
            "rfx_drafter",
            "vendor_dispatcher",
            "document_parser",
            "normalizer",
            "analyst",
        ],
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    events = []
    if STORE_DIR.exists():
        for path in sorted(STORE_DIR.glob("*.json"), reverse=True)[:12]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rfx = data.get("rfx") or {}
                events.append(
                    {
                        "id": rfx.get("rfx_id", path.stem),
                        "title": rfx.get("title", "Untitled"),
                        "step": data.get("wizard_step") or data.get("step", ""),
                        "vendors": len(rfx.get("vendors") or []),
                    }
                )
            except Exception:
                continue
    example = (
        "Corrugated packaging for a snacks plant in Chakan. ~30 SKUs across 3/5/7-ply RSC "
        "cartons, mixed print, annual spend around ₹3.8 crore last year. Need delivered INR "
        "quotes, FSC board, and food-contact certification."
    )
    return _render(request, "crew/index.html", events=events, example_brief=example)


# ── Start / e2e ────────────────────────────────────────────────────────


@app.post("/crew/start", response_class=HTMLResponse)
def crew_start(
    request: Request,
    brief: str = Form(...),
    title: str = Form(""),
    scope: str = Form(""),
    terms: str = Form(""),
):
    if len(brief.strip()) < 10:
        return HTMLResponse(
            "<div class='err'>Please describe the requirement (at least a couple of sentences).</div>",
            status_code=400,
        )
    pipe = RFxPipeline()
    pipe.start_draft(brief, title=title, scope=scope, terms=terms)
    _save(pipe)
    return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}/wizard?step=draft", status_code=303)


@app.post("/crew/draft", response_class=HTMLResponse)
def crew_draft_legacy(request: Request, brief: str = Form(...)):
    """Back-compat: start + generate in one shot."""
    if len(brief.strip()) < 10:
        return HTMLResponse(
            "<div class='err'>Please describe the requirement.</div>",
            status_code=400,
        )
    pipe = RFxPipeline()
    try:
        pipe.draft(brief)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>Draft failed: {exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}/wizard?step=draft", status_code=303)


@app.post("/crew/run-e2e", response_class=HTMLResponse)
def crew_run_e2e(request: Request, brief: str = Form(...)):
    if len(brief.strip()) < 10:
        return HTMLResponse(
            "<div class='err'>Please describe the requirement.</div>",
            status_code=400,
        )
    pipe = RFxPipeline()
    try:
        pipe.run_e2e(brief)
    except Exception as exc:
        return HTMLResponse(
            f"<div class='err'>End-to-end run failed: {exc}</div>",
            status_code=400,
        )
    _save(pipe)
    return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}/wizard?step=award", status_code=303)


@app.post("/api/run-e2e")
def api_run_e2e(brief: str = Form(...)):
    pipe = RFxPipeline()
    try:
        snap = pipe.run_e2e(brief)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    _save(pipe)
    return JSONResponse(
        {"ok": True, "rfx_id": pipe.rfx.rfx_id if pipe.rfx else None, "snapshot": snap}
    )


# ── Workspace shell (free tabs) ────────────────────────────────────────


@app.get("/crew/{rfx_id}", response_class=HTMLResponse)
def crew_board_redirect(rfx_id: str):
    return RedirectResponse(f"/crew/{rfx_id}/wizard", status_code=303)


@app.get("/crew/{rfx_id}/wizard", response_class=HTMLResponse)
def crew_wizard(request: Request, rfx_id: str, step: Optional[str] = None):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if step and step in WIZARD_STEPS:
        pipe.set_wizard_step(step)
        _save(pipe)
    ctx = _wizard_ctx(pipe)
    return _render(request, "crew/wizard.html", **ctx)


@app.post("/crew/{rfx_id}/wizard/goto", response_class=HTMLResponse)
def crew_wizard_goto(rfx_id: str, step: str = Form(...)):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if step not in WIZARD_STEPS:
        return HTMLResponse(f"<div class='err'>Unknown tab: {step}</div>", status_code=400)
    pipe.set_wizard_step(step)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step={pipe.wizard_step}", status_code=303)


@app.post("/crew/{rfx_id}/wizard/next", response_class=HTMLResponse)
def crew_wizard_next(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    pipe.advance()
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step={pipe.wizard_step}", status_code=303)


# ── Draft tab ──────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/draft/fields", response_class=HTMLResponse)
def crew_draft_fields(
    rfx_id: str,
    brief: str = Form(""),
    title: str = Form(""),
    scope: str = Form(""),
    terms: str = Form(""),
):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    pipe.update_draft_fields(
        brief=brief or None,
        title=title or None,
        scope=scope or None,
        terms=terms or None,
    )
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=draft", status_code=303)


@app.post("/crew/{rfx_id}/draft/generate", response_class=HTMLResponse)
def crew_draft_generate(
    rfx_id: str,
    brief: str = Form(""),
    title: str = Form(""),
    scope: str = Form(""),
    terms: str = Form(""),
):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    pipe.update_draft_fields(
        brief=brief or None,
        title=title or None,
        scope=scope or None,
        terms=terms or None,
    )
    try:
        if pipe.rfx.line_items:
            pipe.regenerate_lines(brief=pipe.brief or brief)
        else:
            pipe.draft(pipe.brief or brief)
    except Exception as exc:
        return HTMLResponse(
            f"<div class='err'>Generate failed: {exc}</div>", status_code=400
        )
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=draft", status_code=303)


@app.post("/crew/{rfx_id}/draft/lines", response_class=HTMLResponse)
async def crew_draft_lines(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    form = await request.form()
    # Expect parallel arrays line_id[], description[], qty[], uom[]
    ids = form.getlist("line_id")
    descs = form.getlist("description")
    qtys = form.getlist("qty")
    uoms = form.getlist("uom")
    items = []
    for i in range(max(len(ids), len(descs))):
        items.append(
            {
                "line_id": ids[i] if i < len(ids) else f"L{i+1:02d}",
                "description": descs[i] if i < len(descs) else "",
                "qty": qtys[i] if i < len(qtys) else 0,
                "uom": uoms[i] if i < len(uoms) else "piece",
            }
        )
    try:
        pipe.update_line_items(items)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=draft", status_code=303)


@app.post("/crew/{rfx_id}/draft/continue", response_class=HTMLResponse)
def crew_draft_continue(rfx_id: str):
    """Soft jump to Send (tabs are free; prep cover previews when possible)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if pipe.rfx.line_items:
        try:
            pipe.refresh_cover_previews()
        except Exception:
            pass
    pipe.set_wizard_step("send")
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=send", status_code=303)


# ── Send tab ───────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/dispatch", response_class=HTMLResponse)
def crew_dispatch(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    try:
        pipe.dispatch()
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>Send failed: {exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=send", status_code=303)


@app.post("/crew/{rfx_id}/send/continue", response_class=HTMLResponse)
def crew_send_continue(rfx_id: str):
    """Soft jump to Inbox (optional seed after dispatch)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if pipe.dispatch_log and not pipe.inbox:
        try:
            pipe.seed_inbox()
        except Exception:
            pass
    pipe.set_wizard_step("inbox")
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)


# ── Inbox tab ──────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/inbox/seed", response_class=HTMLResponse)
def crew_inbox_seed(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    pipe.seed_inbox(force=True)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)


@app.post("/crew/{rfx_id}/inbox/parse", response_class=HTMLResponse)
def crew_inbox_parse(rfx_id: str, msg_id: str = Form(...)):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    try:
        pipe.parse_inbox_message(msg_id)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>Parse failed: {exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)


@app.post("/crew/{rfx_id}/inbox/parse-all", response_class=HTMLResponse)
def crew_inbox_parse_all(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    try:
        pipe.parse_all_inbox()
    except Exception as exc:
        return HTMLResponse(
            f"<div class='err'>Parse all failed: {exc}</div>", status_code=400
        )
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)


@app.post("/crew/{rfx_id}/inbox/upload", response_class=HTMLResponse)
async def crew_inbox_upload(
    rfx_id: str,
    files: list[UploadFile] | None = File(None),
    vendor_id: str = Form(""),
):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if files:
        upload_dir = ROOT / "data" / "uploads" / rfx_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if not f.filename:
                continue
            dest = upload_dir / Path(f.filename).name
            dest.write_bytes(await f.read())
            pipe.add_inbox_upload(dest, vendor_id=vendor_id)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)


@app.post("/crew/{rfx_id}/inbox/continue", response_class=HTMLResponse)
def crew_inbox_continue(rfx_id: str):
    """Soft jump to Compare; build matrix only when quotes exist."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if pipe.quotes and not pipe.comparison:
        try:
            pipe.normalize()
        except Exception:
            pass  # empty Compare tab explains what's missing
    pipe.set_wizard_step("compare")
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=compare", status_code=303)


# legacy ingest alias
@app.post("/crew/{rfx_id}/ingest", response_class=HTMLResponse)
async def crew_ingest(
    request: Request,
    rfx_id: str,
    use_samples: str = Form("yes"),
    files: list[UploadFile] | None = File(None),
):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if files and any(f.filename for f in files):
        upload_dir = ROOT / "data" / "uploads" / rfx_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in files:
            if not f.filename:
                continue
            dest = upload_dir / Path(f.filename).name
            dest.write_bytes(await f.read())
            paths.append(dest)
        pipe.ingest(paths=paths)
    else:
        pipe.ingest(directory=VENDOR_DIR)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)


# ── Compare tab ────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/normalize", response_class=HTMLResponse)
def crew_normalize(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    try:
        pipe.normalize()
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=compare", status_code=303)


@app.post("/crew/{rfx_id}/compare/continue", response_class=HTMLResponse)
def crew_compare_continue(rfx_id: str):
    """Soft jump to Ask (no hard gate)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    if pipe.quotes and not pipe.comparison:
        try:
            pipe.normalize()
        except Exception:
            pass
    pipe.set_wizard_step("ask")
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=ask", status_code=303)


# ── Ask tab ────────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/ask", response_class=HTMLResponse)
def crew_ask(request: Request, rfx_id: str, question: str = Form(...)):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    try:
        result = pipe.ask(question)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    if request.headers.get("hx-request"):
        return _render(
            request,
            "crew/partials/chat_turn.html",
            pipe=pipe,
            rfx=pipe.rfx,
            turn=result,
        )
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=ask", status_code=303)


@app.post("/crew/{rfx_id}/ask/continue", response_class=HTMLResponse)
def crew_ask_continue(rfx_id: str):
    """Soft jump to Award (tabs are free)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    pipe.set_wizard_step("award")
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)


# ── Award tab ──────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/award/save", response_class=HTMLResponse)
async def crew_award_save(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    form = await request.form()
    awards: dict[str, str] = {}
    for li in pipe.rfx.line_items:
        vid = str(form.get(f"award_{li.line_id}") or form.get(li.line_id) or "").strip()
        if vid:
            awards[li.line_id] = vid
    try:
        pipe.save_awards(awards)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)


@app.post("/crew/{rfx_id}/award/suggest", response_class=HTMLResponse)
def crew_award_suggest(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    try:
        pipe.suggest_awards()
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)


@app.get("/crew/{rfx_id}/award/print", response_class=HTMLResponse)
def crew_award_print(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    ctx = _wizard_ctx(pipe)
    return _render(request, "crew/award_print.html", **ctx)


@app.get("/crew/{rfx_id}/snapshot")
def crew_snapshot(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return JSONResponse({"error": "not found"}, status_code=404)
    return pipe.snapshot()


@app.get("/crew/{rfx_id}/award/export.xlsx")
def crew_award_export_xlsx(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    data = pipe.export_award("xlsx")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="award_{rfx_id}.xlsx"'},
    )


@app.get("/crew/{rfx_id}/award/export.csv")
def crew_award_export_csv(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    data = pipe.export_award("csv")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="award_{rfx_id}.csv"'},
    )


@app.get("/crew/{rfx_id}/award/export.md")
def crew_award_export_md(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    data = pipe.export_award("md")
    return Response(
        content=data,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="award_{rfx_id}.md"'},
    )


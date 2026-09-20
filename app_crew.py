"""Aerchain RFx Crew — free tabbed workspace (Draft | Send | Inbox | Compare | Ask | Award).

Local:  uvicorn app_crew:app --port 8518 --reload
Vercel: module-level `app` (see vercel.json).
"""
from __future__ import annotations

import html
import json
import logging
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from agents.qualification import eligibility_gaps, questionnaire_matrix  # noqa: E402
from orchestrator.pipeline import (  # noqa: E402
    DATA_ROOT,
    WIZARD_STEPS,
    RFxPipeline,
    STORE_DIR,
    VENDOR_DIR,
    load_pipeline,
)

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
log = logging.getLogger("app_crew")

app = FastAPI(title="Aerchain RFx Crew", version="0.3.0")

# In-memory cache for the warm serverless instance only. Cold starts wipe this;
# load_pipeline rehydrates from STORE_DIR (/tmp) then core.storage Blob when configured.
_SESSIONS: dict[str, RFxPipeline] = {}

_NOT_FOUND_HTML = (
    "<div class='err'><strong>RFx not found</strong>"
    "<p>The session may have expired on a cold start. "
    "Start a new draft from Home.</p></div>"
)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    """Never return bare 'Internal Server Error' text — always a friendly HTML page."""
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    msg = html.escape(str(exc) or exc.__class__.__name__)
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Error — Aerchain RFx Crew</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a}}
 .card{{border:1px solid #f0c0c0;background:#fff5f5;border-radius:8px;padding:1.25rem 1.5rem}}
 h1{{font-size:1.25rem;margin:0 0 .5rem}}
 code{{font-size:.9rem;word-break:break-word}}
 a{{color:#0b5fff}}
</style></head><body>
<div class="card">
 <h1>Something went wrong</h1>
 <p>The action could not be completed. Details:</p>
 <p><code>{msg}</code></p>
 <p><a href="/">← Back to Home</a></p>
</div></body></html>"""
    return HTMLResponse(body, status_code=500)


def _err_html(message: str, status: int = 400) -> HTMLResponse:
    safe = html.escape(str(message))
    return HTMLResponse(f"<div class='err'>{safe}</div>", status_code=status)


def _session(rfx_id: Optional[str] = None) -> RFxPipeline:
    if rfx_id and rfx_id in _SESSIONS:
        return _SESSIONS[rfx_id]
    if rfx_id:
        try:
            pipe = load_pipeline(rfx_id)
        except Exception as exc:
            log.warning("load_pipeline(%s) failed: %s", rfx_id, exc)
            return RFxPipeline()
        if pipe.rfx:
            _SESSIONS[rfx_id] = pipe
            return pipe
    return RFxPipeline()


def _save(pipe: RFxPipeline) -> None:
    """Persist to STORE_DIR; on read-only FS retry under /tmp — never crash the request."""
    if not pipe.rfx:
        return
    _SESSIONS[pipe.rfx.rfx_id] = pipe
    try:
        pipe._persist()
        return
    except OSError as exc:
        log.warning("primary persist failed (%s); retrying under /tmp", exc)
    try:
        fallback = Path("/tmp/aerchain-data/store")
        fallback.mkdir(parents=True, exist_ok=True)
        path = fallback / f"{pipe.rfx.rfx_id}.json"
        path.write_text(
            json.dumps(pipe.snapshot(), indent=2, default=str), encoding="utf-8"
        )
        log.info("saved pipeline %s to %s", pipe.rfx.rfx_id, path)
    except Exception as exc:
        log.exception("fallback persist also failed for %s: %s", pipe.rfx.rfx_id, exc)


def _uploads_dir(rfx_id: str) -> Path:
    d = DATA_ROOT / "uploads" / rfx_id
    d.mkdir(parents=True, exist_ok=True)
    return d


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

    # Compact source-of-truth rows for Compare. Keep the vendor-level preview
    # separate from the cell payload so buyers can orient themselves before
    # opening a particular line.
    vendor_names = {}
    if pipe.rfx:
        vendor_names.update({v.vendor_id: v.name for v in pipe.rfx.vendors})
    if pipe.comparison:
        vendor_names.update(pipe.comparison.vendor_names or {})
    inbox_by_vendor = {}
    for msg in (getattr(pipe, "inbox", None) or []):
        vid = str(getattr(msg, "parsed_vendor_id", "") or getattr(msg, "vendor_id", "") or "")
        if vid and vid not in inbox_by_vendor:
            inbox_by_vendor[vid] = msg
    rfx_line_ids = [li.line_id for li in (pipe.rfx.line_items if pipe.rfx else [])]
    vendor_sources = []
    for q in pipe.quotes or []:
        raw_items = [item for item in (q.raw_evidence or []) if isinstance(item, dict)]
        preview_bits = [
            str(item.get("snippet") or item.get("text") or "").strip()
            for item in raw_items
            if item.get("kind") != "meta" and str(item.get("snippet") or item.get("text") or "").strip()
        ]
        preview = evidence_by_vendor.get(q.vendor_id) or " | ".join(preview_bits) or str(q.notes or "")
        source_file = str(getattr(q, "source_file", "") or "")
        if not source_file:
            for item in raw_items:
                source_file = str(item.get("source_file") or "")
                if source_file:
                    break
        if not source_file and q.vendor_id in inbox_by_vendor:
            source_file = str(getattr(inbox_by_vendor[q.vendor_id], "path", "") or "")
        quoted_line_ids = [
            str(item.get("line_id") or "").strip()
            for item in (q.lines or [])
            if str(item.get("line_id") or "").strip() in rfx_line_ids
        ]
        first_line_id = quoted_line_ids[0] if quoted_line_ids else (rfx_line_ids[0] if rfx_line_ids else "")
        vendor_sources.append({
            "vendor_id": q.vendor_id,
            "vendor_name": vendor_names.get(q.vendor_id, q.vendor_id),
            "source_format": str(q.source_format or "unknown"),
            "source_file": source_file,
            "preview": preview[:220],
            "first_line_id": first_line_id,
        })

    # Premade Ask chips — assignment VP question + ugly-edge / hard cases.
    # Labels are short; `q` is the full prompt sent to the live analyst (never hardcoded answers).
    suggested_questions = [
        {
            "label": "VP: split cheapest / qualified only",
            "q": (
                "What if we split the award, cheapest per line, but only among vendors "
                "who cleared the quality questionnaire?"
            ),
        },
        {
            "label": "Partial coverage (27 of 30)",
            "q": (
                "Which vendors quoted fewer than the full line list, which lines are missing, "
                "and how should that affect a defensible award?"
            ),
        },
        {
            "label": "USD quotes & conversion",
            "q": (
                "Which vendors quoted in USD, how were those prices converted to INR per piece, "
                "and what caveats should a buyer see?"
            ),
        },
        {
            "label": "Per-box vs per-100 UOM",
            "q": (
                "Where do unit-of-measure mismatches appear (per box, per 100 pieces, per kg, "
                "per bundle), what was converted vs left as uom_mismatch, and what is still uncertain?"
            ),
        },
        {
            "label": "When we are not sure",
            "q": (
                "Show every cell or vendor answer the system is not sure about "
                "(uncertain, needs review, missing evidence, or low confidence) and explain "
                "what the buyer should do before awarding."
            ),
        },
        {
            "label": "Knockout failures",
            "q": (
                "Which vendors failed quality questionnaire knockouts, on which questions, "
                "and confirm they must be excluded from a quality-gated award?"
            ),
        },
        {
            "label": "Defendable award to VP",
            "q": (
                "Give a defensible award recommendation I can take to a VP: split vs single-vendor, "
                "totals among questionnaire-cleared vendors only, and the main risks."
            ),
        },
    ]

    shortlist_rows = pipe.shortlist() if pipe.comparison else []
    q_matrix = []
    if pipe.rfx and pipe.comparison:
        try:
            q_matrix = questionnaire_matrix(pipe.rfx, pipe.comparison)
        except Exception:
            q_matrix = []

    shortlist_pass = [r for r in shortlist_rows if r.get("pass")]
    shortlist_fail = [r for r in shortlist_rows if not r.get("pass")]
    ko_matrix = [row for row in q_matrix if row.get("knockout")]
    provisional = pipe.provisional_status()

    # Award explainability — why lines have empty dropdowns / empty Pass shortlist
    award_gaps: dict[str, Any] = {}
    eligibility_reasons_by_line: dict[str, list[str]] = {}
    if pipe.rfx and pipe.comparison:
        try:
            award_gaps = eligibility_gaps(pipe.rfx, pipe.comparison)
            eligibility_reasons_by_line = dict(award_gaps.get("reasons_by_line") or {})
            # Also attach reasons for lines that have zero eligible_by_line entries
            for lid, elig in eligible_by_line.items():
                if elig:
                    continue
                if lid not in eligibility_reasons_by_line:
                    # recompute single-line gaps for this lid
                    one = eligibility_gaps(pipe.rfx, pipe.comparison, line_id=lid)
                    for row in one.get("lines") or []:
                        if row.get("line_id") == lid and row.get("reasons"):
                            eligibility_reasons_by_line[lid] = list(row["reasons"])
        except Exception:
            award_gaps = {}
            eligibility_reasons_by_line = {}

    # Partial award candidates + request status (manager approval workflow)
    partial_by_line: dict[str, list[dict]] = {}
    partial_status_by_line: dict[str, dict] = {}
    if pipe.rfx and pipe.comparison:
        for li in pipe.rfx.line_items:
            try:
                partial_by_line[li.line_id] = pipe.partial_candidates_for_line(li.line_id)
            except Exception:
                partial_by_line[li.line_id] = []
            st = pipe.partial_status_for_line(li.line_id)
            if st:
                partial_status_by_line[li.line_id] = st
    pending_partials = [
        r for r in (getattr(pipe, "partial_requests", None) or [])
        if r.get("status") == "pending"
    ]
    all_partials = list(getattr(pipe, "partial_requests", None) or [])

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
        "vendor_sources": vendor_sources,
        "suggested_questions": suggested_questions,
        "provisional": provisional,
        "shortlist_pass": shortlist_pass,
        "shortlist_fail": shortlist_fail,
        "award_notice_paths": list(getattr(pipe, "award_notice_paths", None) or []),
        "award_log": list(getattr(pipe, "award_log", None) or []),
        "outbox_filter": "",
        "notices_just_sent": 0,
        "q_matrix": q_matrix,
        "ko_matrix": ko_matrix,
        "award_gaps": award_gaps,
        "eligibility_reasons_by_line": eligibility_reasons_by_line,
        "freeze": getattr(pipe, "freeze", None),
        "is_frozen": bool(getattr(pipe, "freeze", None)),
        "review_log": list(getattr(pipe, "review_log", None) or []),
        "partial_by_line": partial_by_line,
        "partial_status_by_line": partial_status_by_line,
        "pending_partials": pending_partials,
        "all_partials": all_partials,
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
    seen: set[str] = set()
    if STORE_DIR.exists():
        for path in sorted(STORE_DIR.glob("*.json"), reverse=True)[:12]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rfx = data.get("rfx") or {}
                rid = rfx.get("rfx_id", path.stem)
                events.append(
                    {
                        "id": rid,
                        "title": rfx.get("title", "Untitled"),
                        "step": data.get("wizard_step") or data.get("step", ""),
                        "vendors": len(rfx.get("vendors") or []),
                    }
                )
                seen.add(rid)
            except Exception:
                continue
    # Blob-backed recent list (survives cold start when /tmp is empty).
    try:
        from core import storage as _storage

        if _storage.backend_name() == "vercel-blob":
            for rid in _storage.list_rfx_ids():
                if rid in seen:
                    continue
                data = _storage.load_state(rid)
                if not data:
                    continue
                rfx = data.get("rfx") or {}
                events.append(
                    {
                        "id": rfx.get("rfx_id", rid),
                        "title": rfx.get("title", "Untitled"),
                        "step": data.get("wizard_step") or data.get("step", ""),
                        "vendors": len(rfx.get("vendors") or []),
                    }
                )
                seen.add(rid)
                if len(events) >= 12:
                    break
    except Exception as exc:
        log.warning("home list_rfx_ids failed: %s", exc)
    # Default happy-path brief — pairs with seed vendor pack after Draft→Send→Seed→Parse.
    # Persona alignment on parse yields ≥2 Pass vendors with usable prices (no hardcoded awards).
    example = (
        "We are a packaged snacks manufacturer with a plant in Chakan (Pune). For the next "
        "financial year we need about 30 SKUs of corrugated packaging: outer shippers for chips "
        "and namkeen (mostly 3-ply, some 5-ply for export and heavier loads), a few die-cut "
        "display trays, and some 7-ply master cartons for palletised export. Total spend last "
        "year was around Rs 3.8 crore. Deliveries weekly to Chakan, prices delivered and "
        "exclusive of GST, 60-day validity, 45-day payment. Print is mostly 1-2 colour flexo. "
        "Build a quality questionnaire with exactly 8 questions covering: ISO 9001 (KO), "
        "FSC Chain of Custody (KO), food-contact/hygiene certification (KO), in-house BCT/ECT "
        "testing, capacity, lead time to first delivery, moisture/contamination control, and "
        "snacks-customer references (3–4 of these must be knockouts)."
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
    try:
        pipe = RFxPipeline()
        pipe.start_draft(brief, title=title, scope=scope, terms=terms)
        _save(pipe)
        return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}/wizard?step=draft", status_code=303)
    except Exception as exc:
        log.exception("crew_start failed")
        return _err_html(f"Could not start draft: {exc}", status=500)


@app.post("/crew/draft", response_class=HTMLResponse)
def crew_draft_legacy(request: Request, brief: str = Form(...)):
    """Back-compat: start + generate in one shot."""
    if len(brief.strip()) < 10:
        return HTMLResponse(
            "<div class='err'>Please describe the requirement.</div>",
            status_code=400,
        )
    try:
        pipe = RFxPipeline()
        pipe.draft(brief)
        _save(pipe)
        return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}/wizard?step=draft", status_code=303)
    except Exception as exc:
        log.exception("crew_draft failed")
        return _err_html(f"Draft failed: {exc}", status=400)


@app.post("/crew/run-e2e", response_class=HTMLResponse)
def crew_run_e2e(request: Request, brief: str = Form(...)):
    if len(brief.strip()) < 10:
        return HTMLResponse(
            "<div class='err'>Please describe the requirement.</div>",
            status_code=400,
        )
    try:
        pipe = RFxPipeline()
        pipe.run_e2e(brief)
        _save(pipe)
        return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}/wizard?step=award", status_code=303)
    except Exception as exc:
        log.exception("crew_run_e2e failed")
        return _err_html(f"End-to-end run failed: {exc}", status=400)


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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    if step and step in WIZARD_STEPS:
        pipe.set_wizard_step(step)
        _save(pipe)
    ctx = _wizard_ctx(pipe)
    filt = (request.query_params.get("filter") or "").strip().lower()
    if filt in ("award", "award_notice"):
        filt = "award_notice"
    elif filt not in ("", "all", "rfq"):
        filt = ""
    ctx["outbox_filter"] = filt
    try:
        ctx["notices_just_sent"] = int(request.query_params.get("notices") or 0)
    except ValueError:
        ctx["notices_just_sent"] = 0
    return _render(request, "crew/wizard.html", **ctx)


@app.post("/crew/{rfx_id}/wizard/goto", response_class=HTMLResponse)
def crew_wizard_goto(rfx_id: str, step: str = Form(...)):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    if step not in WIZARD_STEPS:
        return HTMLResponse(f"<div class='err'>Unknown tab: {step}</div>", status_code=400)
    pipe.set_wizard_step(step)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step={pipe.wizard_step}", status_code=303)


@app.post("/crew/{rfx_id}/wizard/next", response_class=HTMLResponse)
def crew_wizard_next(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        pipe.dispatch()
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=send", status_code=303)
    except Exception as exc:
        log.exception("dispatch failed")
        return _err_html(f"Send failed: {exc}", status=400)


@app.post("/crew/{rfx_id}/send/continue", response_class=HTMLResponse)
def crew_send_continue(rfx_id: str):
    """Soft jump to Inbox (optional seed after dispatch)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        pipe.seed_inbox(force=True)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)
    except Exception as exc:
        log.exception("inbox seed failed")
        return _err_html(f"Seed inbox failed: {exc}", status=500)


@app.post("/crew/{rfx_id}/inbox/parse", response_class=HTMLResponse)
def crew_inbox_parse(rfx_id: str, msg_id: str = Form(...)):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        pipe.parse_inbox_message(msg_id)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)
    except Exception as exc:
        log.exception("parse failed")
        return _err_html(f"Parse failed: {exc}", status=400)


@app.post("/crew/{rfx_id}/inbox/parse-all", response_class=HTMLResponse)
def crew_inbox_parse_all(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        pipe.parse_all_inbox()
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)
    except Exception as exc:
        log.exception("parse-all failed")
        return _err_html(f"Parse all failed: {exc}", status=400)


@app.post("/crew/{rfx_id}/inbox/upload", response_class=HTMLResponse)
async def crew_inbox_upload(
    rfx_id: str,
    files: list[UploadFile] | None = File(None),
    vendor_id: str = Form(""),
):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        if files:
            upload_dir = _uploads_dir(rfx_id)
            for f in files:
                if not f.filename:
                    continue
                dest = upload_dir / Path(f.filename).name
                dest.write_bytes(await f.read())
                pipe.add_inbox_upload(dest, vendor_id=vendor_id)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=inbox", status_code=303)
    except Exception as exc:
        log.exception("inbox upload failed")
        return _err_html(f"Upload failed: {exc}", status=500)


@app.post("/crew/{rfx_id}/inbox/continue", response_class=HTMLResponse)
def crew_inbox_continue(rfx_id: str):
    """Soft jump to Compare; build matrix only when quotes exist."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        if files and any(f.filename for f in files):
            upload_dir = _uploads_dir(rfx_id)
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
    except Exception as exc:
        log.exception("ingest failed")
        return _err_html(f"Ingest failed: {exc}", status=500)



# ── Compare tab ────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/normalize", response_class=HTMLResponse)
def crew_normalize(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        pipe.normalize()
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=compare", status_code=303)
    except Exception as exc:
        log.exception("normalize failed")
        return _err_html(str(exc), status=400)


@app.post("/crew/{rfx_id}/compare/continue", response_class=HTMLResponse)
def crew_compare_continue(rfx_id: str):
    """Soft jump to Ask (no hard gate)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    pipe.set_wizard_step("award")
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)


# ── Award tab ──────────────────────────────────────────────────────────


@app.post("/crew/{rfx_id}/award/save", response_class=HTMLResponse)
async def crew_award_save(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    awards: dict[str, str] = {}
    for li in pipe.rfx.line_items:
        vid = str(form.get(f"award_{li.line_id}") or form.get(li.line_id) or "").strip()
        if vid:
            awards[li.line_id] = vid
    try:
        pipe.save_awards(awards)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)
    except Exception as exc:
        log.exception("award save failed")
        return _err_html(str(exc), status=400)


@app.post("/crew/{rfx_id}/award/suggest", response_class=HTMLResponse)
def crew_award_suggest(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        pipe.suggest_awards()
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)


@app.post("/crew/{rfx_id}/award/partial/request", response_class=HTMLResponse)
async def crew_award_partial_request(request: Request, rfx_id: str):
    """Buyer: request partial award for a non-fully-eligible vendor (needs manager approval)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    line_id = str(form.get("line_id") or "").strip()
    vendor_id = str(form.get("vendor_id") or "").strip()
    note = str(form.get("buyer_note") or form.get("note") or "").strip()
    try:
        pipe.request_partial_award(line_id, vendor_id, note)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)
    except Exception as exc:
        log.exception("partial request failed")
        return _err_html(str(exc), status=400)


@app.post("/crew/{rfx_id}/award/partial/approve", response_class=HTMLResponse)
async def crew_award_partial_approve(request: Request, rfx_id: str):
    """Manager stub: approve a pending partial award request."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    request_id = str(form.get("request_id") or "").strip()
    comment = str(form.get("manager_comment") or form.get("comment") or "").strip()
    try:
        pipe.approve_partial_award(request_id, manager_comment=comment)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)
    except Exception as exc:
        log.exception("partial approve failed")
        return _err_html(str(exc), status=400)


@app.post("/crew/{rfx_id}/award/partial/reject", response_class=HTMLResponse)
async def crew_award_partial_reject(request: Request, rfx_id: str):
    """Manager stub: reject a pending partial award request."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    request_id = str(form.get("request_id") or "").strip()
    comment = str(form.get("manager_comment") or form.get("comment") or "").strip()
    try:
        pipe.reject_partial_award(request_id, manager_comment=comment)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)
    except Exception as exc:
        log.exception("partial reject failed")
        return _err_html(str(exc), status=400)


@app.get("/crew/{rfx_id}/award/print", response_class=HTMLResponse)
def crew_award_print(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    ctx = _wizard_ctx(pipe)
    return _render(request, "crew/award_print.html", **ctx)




@app.get("/crew/{rfx_id}/evidence", response_class=HTMLResponse)
def crew_evidence(request: Request, rfx_id: str, line_id: str = "", vendor_id: str = ""):
    """Evidence drawer partial for a Compare/Award price cell."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    payload = pipe.evidence_for_cell(line_id, vendor_id)
    return _render(
        request,
        "crew/partials/evidence_drawer.html",
        pipe=pipe,
        rfx=pipe.rfx,
        evidence=payload,
    )


@app.post("/crew/{rfx_id}/award/notify", response_class=HTMLResponse)
def crew_award_notify(request: Request, rfx_id: str):
    """Stub-write award_notice_*.txt into data/outbox (no SMTP)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    try:
        paths = pipe.notify_awarded_vendors()
        _save(pipe)
        return RedirectResponse(
            f"/crew/{rfx_id}/wizard?step=send&filter=award&notices={len(paths)}",
            status_code=303,
        )
    except Exception as exc:
        log.exception("award notify failed")
        return _err_html(str(exc), status=400)


@app.post("/crew/{rfx_id}/award/freeze", response_class=HTMLResponse)
async def crew_award_freeze(request: Request, rfx_id: str):
    """Persist freeze snapshot; lock award dropdowns until unfreeze."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    note = str(form.get("note") or "").strip()
    try:
        pipe.freeze_award(note=note)
        _save(pipe)
        return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)
    except Exception as exc:
        log.exception("freeze failed")
        return _err_html(str(exc), status=400)


@app.post("/crew/{rfx_id}/award/unfreeze", response_class=HTMLResponse)
async def crew_award_unfreeze(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    note = str(form.get("note") or "").strip()
    try:
        pipe.unfreeze_award(note=note)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{rfx_id}/wizard?step=award", status_code=303)


@app.post("/crew/{rfx_id}/review/override", response_class=HTMLResponse)
async def crew_cell_override(request: Request, rfx_id: str):
    """Cheap cell override — appends review_log only (no cell status mutation)."""
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    form = await request.form()
    line_id = str(form.get("line_id") or "").strip()
    vendor_id = str(form.get("vendor_id") or "").strip()
    note = str(form.get("note") or "").strip()
    try:
        pipe.log_cell_override(line_id, vendor_id, note)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>{exc}</div>", status_code=400)
    _save(pipe)
    # Refresh evidence drawer if HTMX, else back to compare
    hx = request.headers.get("HX-Request")
    if hx:
        payload = pipe.evidence_for_cell(line_id, vendor_id)
        return _render(
            request,
            "crew/partials/evidence_drawer.html",
            pipe=pipe,
            rfx=pipe.rfx,
            evidence=payload,
            review_log=list(pipe.review_log or []),
            override_ok=True,
        )
    return RedirectResponse(
        f"/crew/{rfx_id}/wizard?step=compare", status_code=303
    )


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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
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
        return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
    data = pipe.export_award("md")
    return Response(
        content=data,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="award_{rfx_id}.md"'},
    )


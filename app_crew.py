"""Aerchain RFx Crew — FastAPI entrypoint for the 5-agent pipeline.

Local:  uvicorn app_crew:app --port 8518 --reload
Vercel: detects module-level `app` in app_crew.py (see vercel.json).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from orchestrator.pipeline import (  # noqa: E402
    RFxPipeline,
    STORE_DIR,
    VENDOR_DIR,
    load_pipeline,
)

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="Aerchain RFx Crew", version="0.2.0")

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
    # Starlette >=0.37: TemplateResponse(request, name, context)
    return templates.TemplateResponse(request, name, ctx)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "app": "rfx-crew",
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
                        "step": data.get("step", ""),
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


@app.post("/crew/draft", response_class=HTMLResponse)
def crew_draft(request: Request, brief: str = Form(...)):
    if len(brief.strip()) < 20:
        return HTMLResponse(
            "<div class='err'>Please describe the requirement in at least a couple of sentences.</div>",
            status_code=400,
        )
    pipe = RFxPipeline()
    try:
        pipe.draft(brief)
    except Exception as exc:
        return HTMLResponse(f"<div class='err'>Draft failed: {exc}</div>", status_code=400)
    _save(pipe)
    return RedirectResponse(f"/crew/{pipe.rfx.rfx_id}", status_code=303)


@app.get("/crew/{rfx_id}", response_class=HTMLResponse)
def crew_board(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    return _render(
        request,
        "crew/board.html",
        pipe=pipe,
        rfx=pipe.rfx,
        snapshot=pipe.snapshot(),
        vendor_dir=str(VENDOR_DIR),
    )


@app.post("/crew/{rfx_id}/dispatch", response_class=HTMLResponse)
def crew_dispatch(request: Request, rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return HTMLResponse("RFx not found", status_code=404)
    pipe.dispatch()
    _save(pipe)
    if request.headers.get("hx-request"):
        return _render(request, "crew/partials/dispatch.html", pipe=pipe, rfx=pipe.rfx)
    return RedirectResponse(f"/crew/{rfx_id}", status_code=303)


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
    if request.headers.get("hx-request"):
        return _render(request, "crew/partials/quotes.html", pipe=pipe, rfx=pipe.rfx)
    return RedirectResponse(f"/crew/{rfx_id}", status_code=303)


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
    if request.headers.get("hx-request"):
        return _render(request, "crew/partials/matrix.html", pipe=pipe, rfx=pipe.rfx)
    return RedirectResponse(f"/crew/{rfx_id}", status_code=303)


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
    return _render(
        request,
        "crew/partials/answer.html",
        pipe=pipe,
        rfx=pipe.rfx,
        question=question,
        result=result,
    )


@app.get("/crew/{rfx_id}/snapshot")
def crew_snapshot(rfx_id: str):
    pipe = _session(rfx_id)
    if not pipe.rfx:
        return JSONResponse({"error": "not found"}, status_code=404)
    return pipe.snapshot()

"""Kill the Quote Spreadsheet: FastAPI entrypoint.

Vercel detects the module-level `app`. Locally: `uvicorn app:app --port 8517 --reload`.
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse, Response  # noqa: E402

from core import ingest, llm, rfx_drafter, storage, vendor_sim  # noqa: E402
from core.web import error_fragment, hx_redirect, load_or_404, now_iso, render  # noqa: E402
from routes import ask, compare, inbox  # noqa: E402

app = FastAPI(title="Kill the Quote Spreadsheet")
app.include_router(inbox.router)
app.include_router(compare.router)
app.include_router(ask.router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Surface the failure in the page (HTMX fragments included) instead of a bare 500."""
    import traceback

    tb = traceback.format_exc(limit=3)
    return error_fragment(f"{type(exc).__name__}: {exc}<pre class='mt-2 text-xs whitespace-pre-wrap'>{tb[-1200:]}</pre>", 500)


# ---------------------------------------------------------------------------
# Home / Brief
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    events = []
    for rid in storage.list_rfx_ids()[:12]:
        st = storage.load_state(rid)
        if st:
            events.append(
                {
                    "id": rid,
                    "title": st["rfx"].get("title", "Untitled RFx"),
                    "created_at": st.get("created_at", ""),
                    "status": st.get("status", "draft"),
                    "vendors": len(st.get("vendors", [])),
                    "extracted": sum(1 for v in st.get("vendors", []) if v.get("status") == "extracted"),
                }
            )
    return render(request, "index.html", events=events, example_brief=vendor_sim.EXAMPLE_BRIEF)


@app.post("/draft", response_class=HTMLResponse)
def draft(request: Request, brief: str = Form(...)):
    if len(brief.strip()) < 20:
        return error_fragment("Please describe the requirement in at least a sentence or two.", 400)
    try:
        log: list = []
        rfx = rfx_drafter.draft_rfx(brief, log=log)
        state = rfx_drafter.new_state(brief, rfx)
        state["ai_log"] = log
        storage.save_state(state["id"], state)
    except llm.AINotConfigured as e:
        return error_fragment(str(e), 400)
    except Exception as e:  # surfaced to the user, never swallowed
        return error_fragment(f"Drafting failed: {e}")
    return hx_redirect(f"/rfx/{state['id']}/rfx")


@app.post("/rfx/{rfx_id}/delete")
def delete_rfx(rfx_id: str):
    storage.delete_rfx(rfx_id)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# RFx page
# ---------------------------------------------------------------------------

@app.get("/rfx/{rfx_id}/rfx", response_class=HTMLResponse)
def rfx_page(request: Request, rfx_id: str):
    state = load_or_404(rfx_id)
    return render(request, "rfx.html", state=state, rfx=state["rfx"], active="rfx", vendors=vendor_sim.VENDORS)


@app.post("/rfx/{rfx_id}/line/{line_no}", response_class=HTMLResponse)
def update_line(
    request: Request, rfx_id: str, line_no: int, description: str = Form(...), annual_qty: int = Form(...), gsm: int = Form(...)
):
    state = load_or_404(rfx_id)
    for li in state["rfx"]["line_items"]:
        if li["line_no"] == line_no:
            li["description"] = description.strip()
            li["annual_qty"] = max(1, annual_qty)
            li["gsm"] = max(100, gsm)
            li["nominal_weight_g"] = rfx_drafter.nominal_weight_g(li["length_mm"], li["width_mm"], li["height_mm"], li["gsm"])
            storage.save_state(rfx_id, state)
            return render(request, "partials/line_row.html", state=state, li=li, saved=True)
    raise HTTPException(404)


@app.post("/rfx/{rfx_id}/send", response_class=HTMLResponse)
def send_rfx(request: Request, rfx_id: str):
    state = load_or_404(rfx_id)
    if not state["outbox"]:
        for v in vendor_sim.VENDORS:
            state["outbox"].append(
                {
                    "kind": "rfx",
                    "to": v["email"],
                    "vendor_id": v["vendor_id"],
                    "vendor_name": v["name"],
                    "subject": f"RFQ: {state['rfx']['title']} ({rfx_id})",
                    "body": vendor_sim.cover_email(state["rfx"], v),
                    "sent_at": now_iso(),
                    "delivery": "stubbed (no SMTP)",
                }
            )
        if not state["vendors"]:
            state["vendors"] = [vendor_sim.empty_vendor(v) for v in vendor_sim.VENDORS]
        state["status"] = "sent"
        storage.save_state(rfx_id, state)
    return hx_redirect(f"/rfx/{rfx_id}/inbox")


@app.get("/rfx/{rfx_id}/outbox", response_class=HTMLResponse)
def outbox(request: Request, rfx_id: str):
    state = load_or_404(rfx_id)
    return render(request, "outbox.html", state=state, active="inbox")


@app.get("/rfx/{rfx_id}/ai-log", response_class=HTMLResponse)
def ai_log(request: Request, rfx_id: str):
    state = load_or_404(rfx_id)
    return render(request, "ai_log.html", state=state, active="award")


# ---------------------------------------------------------------------------
# Files (local backend only; Blob URLs are served by Vercel directly)
# ---------------------------------------------------------------------------

@app.get("/files/{path:path}")
def serve_local_file(path: str):
    data = storage.get_bytes(f"/files/{path}")
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type=ingest.guess_content_type(path))


@app.get("/healthz")
def healthz(fingerprint: bool = False):
    """Liveness plus, on request, a SHA-1 per source file so a deployment can be verified against the repo."""
    out = {"ok": True, "ai": llm.is_configured(), "model": llm.model_name(), "storage": storage.backend_name()}
    if fingerprint:
        import hashlib
        from pathlib import Path

        root = Path(__file__).resolve().parent
        files = [root / "app.py", root / "requirements.txt", root / "vercel.json"] + sorted((root / "core").glob("*.py")) + sorted((root / "routes").glob("*.py")) + sorted((root / "templates").rglob("*.html"))
        out["files"] = {str(p.relative_to(root)): hashlib.sha1(p.read_bytes()).hexdigest() for p in files if p.exists()}
    return out

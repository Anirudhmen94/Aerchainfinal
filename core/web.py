"""Shared web helpers: templates, rendering context, state loading."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import markdown as md
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import engine, llm, storage

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["md"] = lambda text: md.markdown(text or "", extensions=["tables", "fenced_code"])
templates.env.filters["inr"] = lambda v: engine.fmt_inr(v)
templates.env.filters["num"] = lambda v: engine.fmt_num(v)
templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False, default=str)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    state = ctx.get("state")
    base = {
        "request": request,
        "ai_ok": llm.is_configured(),
        "model": llm.model_name(),
        "storage": storage.backend_name(),
        "rfx_id": state.get("id") if isinstance(state, dict) else None,
    }
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


def load_or_404(rfx_id: str) -> dict:
    state = storage.load_state(rfx_id)
    if not state:
        raise HTTPException(404, "RFx not found")
    return state


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def error_fragment(message: str, status: int = 500) -> HTMLResponse:
    html = (
        '<div class="rounded-lg border border-rose-200 bg-rose-50 text-rose-800 text-sm p-4">'
        f"<strong>Something went wrong.</strong> {message}</div>"
    )
    return HTMLResponse(html, status_code=status)


def hx_redirect(url: str) -> HTMLResponse:
    resp = HTMLResponse("")
    resp.headers["HX-Redirect"] = url
    return resp

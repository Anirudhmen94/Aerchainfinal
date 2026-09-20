"""Persistence layer with two interchangeable backends.

- Local filesystem (data/store/) when BLOB_READ_WRITE_TOKEN is not set.
- Vercel Blob (REST API) when it is. Vercel functions are stateless, so all
  state must live outside the function instance.

State documents are written as immutable, timestamped versions and resolved via
list() rather than by overwriting a fixed pathname. Blob URLs sit behind a CDN
that may serve a stale copy for up to a minute after an overwrite; versioned
writes sidestep that entirely.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

BLOB_API = os.environ.get("VERCEL_BLOB_API_URL", "https://vercel.com/api/blob")
BLOB_API_VERSION = "12"
LOCAL_ROOT = Path(os.environ.get("LOCAL_STORE_DIR", "data/store"))


def _token() -> str | None:
    return os.environ.get("BLOB_READ_WRITE_TOKEN") or None


def _headers(extra: dict | None = None) -> dict:
    """Auth headers the Blob API expects (mirrors @vercel/blob v12: bearer token + store id parsed from it)."""
    token = _token() or ""
    parts = token.split("_")
    store_id = parts[3] if len(parts) > 3 else ""
    h = {"authorization": f"Bearer {token}", "x-api-version": BLOB_API_VERSION, "x-vercel-blob-store-id": store_id}
    if extra:
        h.update(extra)
    return h


def backend_name() -> str:
    return "vercel-blob" if _token() else "local-files"


def _safe(path: str) -> str:
    path = path.strip("/")
    if ".." in path.split("/"):
        raise ValueError("invalid path")
    return path


# ---------------------------------------------------------------------------
# Raw bytes
# ---------------------------------------------------------------------------

def put_bytes(path: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Store bytes at `path`. Returns a URL the app can later hand to get_bytes()."""
    path = _safe(path)
    token = _token()
    if not token:
        target = LOCAL_ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return f"/files/{path}"

    headers = _headers(
        {
            "x-vercel-blob-access": "public",
            "x-content-type": content_type,
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
            "x-cache-control-max-age": "60",
        }
    )
    with httpx.Client(timeout=120) as client:
        r = client.put(f"{BLOB_API}/", params={"pathname": path}, content=data, headers=headers)
        r.raise_for_status()
        return r.json()["url"]


def get_bytes(url: str) -> bytes | None:
    if url.startswith("/files/"):
        target = LOCAL_ROOT / _safe(url[len("/files/"):])
        return target.read_bytes() if target.exists() else None
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content


def list_paths(prefix: str) -> list[dict[str, Any]]:
    """Return [{pathname, url, uploaded_at}] under prefix."""
    prefix = _safe(prefix)
    token = _token()
    if not token:
        root = LOCAL_ROOT / prefix
        out = []
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    rel = p.relative_to(LOCAL_ROOT).as_posix()
                    out.append({"pathname": rel, "url": f"/files/{rel}", "uploaded_at": p.stat().st_mtime})
        return out
    out = []
    cursor = None
    with httpx.Client(timeout=60) as client:
        while True:
            params = {"prefix": prefix, "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            r = client.get(f"{BLOB_API}/", params=params, headers=_headers())
            r.raise_for_status()
            body = r.json()
            for b in body.get("blobs", []):
                out.append({"pathname": b["pathname"], "url": b["url"], "uploaded_at": b.get("uploadedAt")})
            if body.get("hasMore") and body.get("cursor"):
                cursor = body["cursor"]
            else:
                break
    return out


def delete_urls(urls: list[str]) -> None:
    if not urls:
        return
    token = _token()
    if not token:
        for u in urls:
            if u.startswith("/files/"):
                p = LOCAL_ROOT / _safe(u[len("/files/"):])
                if p.exists():
                    p.unlink()
        return
    with httpx.Client(timeout=60) as client:
        client.post(f"{BLOB_API}/delete", json={"urls": urls}, headers=_headers())


# ---------------------------------------------------------------------------
# Versioned JSON state documents
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"/state/(\d+)\.json$")


def save_state(rfx_id: str, state: dict[str, Any]) -> None:
    version = int(time.time() * 1000)
    state["_version"] = version
    payload = json.dumps(state, ensure_ascii=False, default=str).encode("utf-8")
    put_bytes(f"rfx/{rfx_id}/state/{version}.json", payload, "application/json")
    # Prune older versions, keeping the newest few for safety.
    versions = sorted(list_paths(f"rfx/{rfx_id}/state/"), key=lambda b: b["pathname"])
    stale = versions[:-3]
    try:
        delete_urls([b["url"] for b in stale])
    except Exception:
        pass


def load_state(rfx_id: str) -> dict[str, Any] | None:
    versions = list_paths(f"rfx/{rfx_id}/state/")
    if not versions:
        return None
    latest = max(versions, key=lambda b: int(_VERSION_RE.search("/" + b["pathname"]).group(1)) if _VERSION_RE.search("/" + b["pathname"]) else 0)
    raw = get_bytes(latest["url"])
    return json.loads(raw) if raw else None


def list_rfx_ids() -> list[str]:
    ids: dict[str, int] = {}
    for b in list_paths("rfx/"):
        m = re.match(r"rfx/([^/]+)/state/(\d+)\.json$", b["pathname"])
        if m:
            ids[m.group(1)] = max(ids.get(m.group(1), 0), int(m.group(2)))
    return [k for k, _ in sorted(ids.items(), key=lambda kv: -kv[1])]


def delete_rfx(rfx_id: str) -> None:
    delete_urls([b["url"] for b in list_paths(f"rfx/{rfx_id}/")])

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
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Prefer the documented vercel.com host; keep env override for tests/mirrors.
BLOB_API = os.environ.get("VERCEL_BLOB_API_URL", "https://vercel.com/api/blob")
BLOB_API_VERSION = "12"


def _local_store_root() -> Path:
    if os.environ.get("LOCAL_STORE_DIR"):
        return Path(os.environ["LOCAL_STORE_DIR"])
    override = os.environ.get("AERCHAIN_DATA_ROOT", "").strip()
    if override:
        return Path(override) / "store"
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        return Path("/tmp/aerchain-data/store")
    return Path("data/store")


LOCAL_ROOT = _local_store_root()


def _token() -> str | None:
    raw = (os.environ.get("BLOB_READ_WRITE_TOKEN") or "").strip()
    if not raw:
        return None
    # Encrypted Vercel env envelopes (JSON v2 / base64 "eyJ2...") are not Bearer tokens.
    if raw.startswith("eyJ") or raw.startswith("{"):
        log.error(
            "BLOB_READ_WRITE_TOKEN looks like an encrypted envelope, not a vercel_blob_rw_* token"
        )
        return None
    return raw


def parse_store_id(token: str | None = None) -> str | None:
    """Extract Blob store id from token or env.

    Token shape: vercel_blob_rw_<STORE_ID>_<SECRET...>
    Store id is the 4th underscore-separated segment when that prefix matches.
    Falls back to BLOB_STORE_ID / VERCEL_BLOB_STORE_ID.
    """
    tok = (token if token is not None else _token()) or ""
    if tok.startswith("vercel_blob_rw_"):
        parts = tok.split("_")
        # vercel, blob, rw, STORE_ID, SECRET...
        if len(parts) > 3 and parts[3]:
            return parts[3]
    for key in ("BLOB_STORE_ID", "VERCEL_BLOB_STORE_ID"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


def _headers(extra: dict | None = None) -> dict:
    """Auth headers the Blob API expects (mirrors @vercel/blob v12).

    Omit x-vercel-blob-store-id when we cannot resolve a store id — some token
    shapes carry it implicitly; sending an empty string yields 403
    "Cannot get store id from token or header".
    """
    token = _token() or ""
    h: dict[str, str] = {
        "authorization": f"Bearer {token}",
        "x-api-version": BLOB_API_VERSION,
    }
    store_id = parse_store_id(token)
    if store_id:
        h["x-vercel-blob-store-id"] = store_id
    if extra:
        h.update(extra)
    return h


def blob_configured() -> bool:
    return bool(_token())


def backend_name() -> str:
    return "vercel-blob" if blob_configured() else "local-files"


def _safe(path: str) -> str:
    path = path.strip("/")
    if ".." in path.split("/"):
        raise ValueError("invalid path")
    return path


def _raise_blob_http(r: httpx.Response, op: str) -> None:
    """Raise with a clear message on store-id / auth failures."""
    if r.status_code == 403:
        body = (r.text or "")[:300]
        store = parse_store_id()
        log.error(
            "Blob %s 403 Forbidden (store_id=%r). "
            "Cannot get store id from token or header? body=%s",
            op,
            store,
            body,
        )
        raise httpx.HTTPStatusError(
            f"Blob {op} 403: Cannot get store id from token or header "
            f"(resolved store_id={store!r}). Set BLOB_STORE_ID or use a "
            f"vercel_blob_rw_* token. body={body}",
            request=r.request,
            response=r,
        )
    r.raise_for_status()


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

    # Private stores reject public access (HTTP 400). Default private; override via BLOB_ACCESS.
    access = (os.environ.get("BLOB_ACCESS") or "private").strip().lower()
    if access not in ("private", "public"):
        access = "private"
    headers = _headers(
        {
            "x-vercel-blob-access": access,
            "x-content-type": content_type,
            "x-add-random-suffix": "0",
            "x-allow-overwrite": "1",
            "x-cache-control-max-age": "60",
        }
    )
    # Prefer documented put URL; fall back to trailing-slash variant.
    put_urls = [f"{BLOB_API}", f"{BLOB_API}/"]
    last_exc: Exception | None = None
    with httpx.Client(timeout=120) as client:
        for url in put_urls:
            try:
                r = client.put(url, params={"pathname": path}, content=data, headers=headers)
                if r.status_code in (404, 405) and url != put_urls[-1]:
                    continue
                _raise_blob_http(r, "PUT")
                return r.json()["url"]
            except httpx.HTTPStatusError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
    if last_exc:
        raise last_exc
    raise RuntimeError("Blob PUT failed with no response")


def get_bytes(url: str) -> bytes | None:
    if url.startswith("/files/"):
        target = LOCAL_ROOT / _safe(url[len("/files/"):])
        return target.read_bytes() if target.exists() else None
    # Private blobs are not anonymously readable — auth like other Blob API calls.
    headers = _headers() if blob_configured() else None
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        r = client.get(url, headers=headers)
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
            _raise_blob_http(r, "LIST")
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
        r = client.post(f"{BLOB_API}/delete", json={"urls": urls}, headers=_headers())
        if r.status_code >= 400:
            _raise_blob_http(r, "DELETE")


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
    latest = max(
        versions,
        key=lambda b: int(_VERSION_RE.search("/" + b["pathname"]).group(1))
        if _VERSION_RE.search("/" + b["pathname"])
        else 0,
    )
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


def storage_healthcheck() -> dict[str, Any]:
    """Tiny write/read roundtrip for /healthz/storage. Never raises.

    When Vercel Blob is configured but suspended/403, we still probe the local
    (/tmp) backend and document that durable cold-start recovery is via
    browser localStorage + POST /crew/rehydrate — not Blob.
    """
    result: dict[str, Any] = {
        "backend": backend_name(),
        "blob_configured": blob_configured(),
        "blob_ok": False,
        "write_ok": False,
        "read_ok": False,
        "fallback": None,
    }
    probe_id = f"_healthz_{int(time.time() * 1000)}"
    blob_err: str | None = None

    # 1) Try configured Blob (if any)
    if blob_configured():
        try:
            payload = {"ok": True, "probe": probe_id, "via": "blob"}
            # Force blob path: save_state uses put_bytes which uses token
            save_state(probe_id, dict(payload))
            loaded = load_state(probe_id)
            if loaded and loaded.get("probe") == probe_id:
                result["blob_ok"] = True
                result["write_ok"] = True
                result["read_ok"] = True
                result["backend"] = "vercel-blob"
                try:
                    delete_rfx(probe_id)
                except Exception:
                    pass
                return result
        except Exception as exc:  # noqa: BLE001
            blob_err = f"{exc.__class__.__name__}: {exc}"
            log.warning("storage_healthcheck blob failed: %s", exc)
            result["blob_error"] = blob_err
            if "store_suspended" in str(exc):
                result["blob_status"] = "store_suspended"
                result["store_suspended"] = True

    # 2) Local /tmp (or data/store) roundtrip — warm-instance durability only
    token_was = os.environ.get("BLOB_READ_WRITE_TOKEN")
    try:
        # Temporarily disable blob so put/get hit LOCAL_ROOT
        if "BLOB_READ_WRITE_TOKEN" in os.environ:
            os.environ.pop("BLOB_READ_WRITE_TOKEN", None)
        # Re-bind is not needed: _token() reads env each call
        local_probe = f"{probe_id}_local"
        payload = {"ok": True, "probe": local_probe, "via": "local"}
        save_state(local_probe, dict(payload))
        loaded = load_state(local_probe)
        result["write_ok"] = bool(loaded and loaded.get("probe") == local_probe)
        result["read_ok"] = result["write_ok"]
        result["backend"] = "local-tmp" if (os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")) else "local-files"
        try:
            delete_rfx(local_probe)
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        log.warning("storage_healthcheck local failed: %s", exc)
    finally:
        if token_was is not None:
            os.environ["BLOB_READ_WRITE_TOKEN"] = token_was

    # 3) Document client fallback when Blob is dead (the demo cold-start path)
    if blob_configured() and not result.get("blob_ok"):
        result["fallback"] = "client_localStorage+rehydrate"
        result["durable"] = False
        result["note"] = (
            "Vercel Blob unavailable (often store_suspended). "
            "Instance /tmp is warm-only; cold starts recover via "
            "browser localStorage key aerchain.rfx.{id} → POST /crew/rehydrate."
        )
    else:
        result["durable"] = bool(result.get("blob_ok") or result.get("backend") == "local-files")
        if result.get("backend") == "local-files":
            result["fallback"] = None
    return result

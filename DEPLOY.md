# Deploy notes — new shareable URL

This crew app must be a **new** Vercel project. Do **not** point the old
`kill-the-quote-spreadsheet` Vercel project at this repo.

## Entrypoint

- File: `app_crew.py`
- ASGI app object: `app`
- `vercel.json` routes traffic to `app_crew.py` with `maxDuration: 300`

## Steps

1. Vercel → Add New Project → import `Anirudhmen94/Aerchainfinal`.
2. Root directory: repository root.
3. Environment variables:
   - `ANTHROPIC_API_KEY` (required for LLM draft / unstructured parse)
   - optional `ANTHROPIC_HAIKU_MODEL` / `ANTHROPIC_SONNET_MODEL`
4. Deploy.
5. Smoke check: `GET /healthz` → `{"ok": true, "app": "rfx-crew", ...}`.

## Local parity

```bash
uvicorn app_crew:app --host 0.0.0.0 --port 8518
```

Ephemeral serverless state: prefer `data/store/` snapshots or attach Blob later;
the demo UI also keeps an in-process session map for warm instances.

## Storage note (Sep 2026)

Production Blob (`BLOB_READ_WRITE_TOKEN`) currently returns **`store_suspended`** —
`/healthz/storage` shows `blob_ok=false`, `write_ok` only for warm `/tmp`.

Until a fresh `vercel_blob_rw_*` token / unsuspended store is configured:

- Prefer the **durable demo** (Cloudflare tunnel → local uvicorn on `:8518` writing under `/workspace`).
- On Vercel, client backup is **embedded** `window.__AERCHAIN_SNAP__` / `#aerchain-snap` → `localStorage` key `aerchain.rfx.{id}` → `POST /crew/rehydrate` on cold 404.
- Home page shows a storage warning banner when Blob is configured but not writable.


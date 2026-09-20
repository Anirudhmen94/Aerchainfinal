# Demo script — RFx Crew

~10–12 minutes plus model latency. Stub dispatch/parse(JSON·CSV)/normalize is near-instant.

## 0. Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY for live draft + unstructured parse
uvicorn app_crew:app --port 8518 --reload
```

Open http://127.0.0.1:8518 — header chips show the five agents.
`/healthz` should report `"app": "rfx-crew"`.

## 1. Brief → Drafter (1–2 min)

Home page. Read the pre-filled Chakan snacks / corrugated brief. Click **Draft RFx**.
On the board: line IDs, knockout questionnaire, five vendors. Note `rfx_id` — snapshots
land in `data/store/`.

## 2. Dispatcher (1 min)

Click **Send to N vendors**. Outbox panel (and `data/outbox/`) shows one stub email per
vendor. Nothing hit SMTP; the artefact is the point.

## 3. Parser / ingest (2 min)

Click **Ingest sample files**. Walk samples under `data/vendor_responses/` — JSON per-100,
CSV with gaps, USD/1000 text, bundle rate card, per-kg email. Confidence and row counts
show on each card. Optional: upload your own file.

## 4. Normalizer (2–3 min)

Click **Build comparison**. Walk `converted` / `missing` / `uncertain` / `uom_mismatch`
cells and vendor flags under the table. No silent arithmetic.

## 5. Analyst (3–4 min)

Ask, in order:

1. **“Cheapest per line”**
2. **“Vendor totals”**
3. **“Where are the gaps?”**
4. Free-form award / risk question

Expand **Data used** under an answer to show helper output.

## 6. Close (1 min)

Show `/crew/{id}/snapshot` JSON (contracts match `shared_models`). Mention `DECISIONS.md`:
why five agents, why FastAPI, why a **new** Vercel URL for `app_crew.py`.

## If asked “is this hardcoded?”

Change the brief, re-draft, re-ingest. Line IDs and matrix shift with the drafted RFx;
rankings recompute from cells — no canned award paragraph.

# Aerchainfinal — RFx Crew (free tabbed workspace)

Evidence-minded sourcing prototype for corrugated packaging. A buyer works in a
**free tabbed workspace**: Draft | Send | Inbox | Compare | Ask | Award. Open any
tab anytime — empty panels explain what’s missing (no unlock gates). Five agents
power each stage; the orchestrator persists state under `data/store/`.

Repository: https://github.com/Anirudhmen94/Aerchainfinal

Entrypoint: `app_crew:app` (not the older kill-the-quote-spreadsheet project).

## Workspace tabs

| Tab | What the buyer does | Agent |
|---|---|---|
| **Draft** | Edit brief + title/scope/terms → **Generate line items** (30 lines + questionnaire + vendors) → tweak lines | RFx Drafter |
| **Send** | Review cover email previews → **Send to vendors** → outbox confirmation | Vendor Dispatcher (SMTP stubbed → `data/outbox/`) |
| **Inbox** | Seed/upload stub replies → **Parse** / **Parse all** → quotations + questionnaire answers | Document Parser |
| **Compare** | Side-by-side INR matrix, coverage, knockout pass/fail badges; only qualified vendors are award-eligible | Normalizer + qualification |
| **Ask** | Persistent live chat with the Analyst (history kept) | Analyst |
| **Award** | Per-line dropdown (qualified vendors with a price) → save → summary / print | Analyst `validate_award` / `suggest_split_award` |

Navigation is free — no unlock gates. Empty panels explain what’s missing.
A dismissible **Quick start** tip on the home page is optional (localStorage).
An optional **auto-run demo (e2e)** link on the home page still exists for one-shot demos.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY
uvicorn app_crew:app --port 8518 --reload
```

Open http://127.0.0.1:8518 — start a draft and use any tab.

Without `ANTHROPIC_API_KEY`, structured JSON/CSV parse + normalize + offline analyst
still work once an RFx exists; drafting and unstructured parse need the key.

## Deploy

`vercel.json` targets **`app_crew.py`**. Import this repo into a **new** Vercel
project (do not reuse kill-the-quote-spreadsheet). Set `ANTHROPIC_API_KEY`.
Verify `/healthz` returns `"app": "rfx-crew"`. See `DEPLOY.md`.

## Layout

```
agents/                 Five crew agents + qualification.py
orchestrator/           Tab-aware pipeline + persistence
shared_models.py        Pydantic contracts (incl. awards / inbox / knockouts)
app_crew.py             FastAPI workspace routes
templates/crew/         Tabbed UI (wizard.html)
data/vendor_responses/  Ugly-edge binaries (xlsx/pdf/docx/png/email) + extracts
data/fixtures/           Generator for those samples
data/inbox/             Stub inbound mail (per RFx)
data/outbox/            Stub dispatch emails
data/store/             Session snapshots (gitignored)
```

## Deliberately not built

Real SMTP/IMAP, vendor portal, ERP hand-off, multi-user auth. Arithmetic and
ranking stay in code; the Analyst explains over computed tables.


## Ugly edges (seeded inbox)

Real binary samples under `data/vendor_responses/` (regenerate with
`python data/fixtures/generate_ugly_edges.py`):

1. **V01** Excel ignoring the template — weird columns, per-100 pcs
2. **V02** PDF letterhead — footnote discount, ~27/30 lines
3. **V03** Word prose — USD / per 1000
4. **V04** Angled PNG rate card — per-box / per-bundle
5. **V05** One-line email — ₹/kg + “same as last year”, freight extra

Compare colors cell statuses `ok|converted|missing|uncertain|uom_mismatch`.
Award exports Excel/CSV/Markdown. Ask chips include the VP-defend question.

See `DECISIONS.md` and `DEMO_SCRIPT.md`.

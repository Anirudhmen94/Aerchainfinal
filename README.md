# Aerchainfinal — RFx Crew (live product wizard)

Evidence-minded sourcing prototype for corrugated packaging. A buyer walks a
**sequential wizard**: Draft → Send → Inbox → Compare → Ask → Award. Five agents
power each stage; the orchestrator persists state under `data/store/`.

Repository: https://github.com/Anirudhmen94/Aerchainfinal

Entrypoint: `app_crew:app` (not the older kill-the-quote-spreadsheet project).

## Wizard steps

| Step | What the buyer does | Agent |
|---|---|---|
| **Draft** | Edit brief + title/scope/terms → **Generate line items** (30 lines + questionnaire + vendors) → tweak lines → Continue | RFx Drafter |
| **Send** | Review cover email previews → **Send to vendors** → outbox confirmation | Vendor Dispatcher (SMTP stubbed → `data/outbox/`) |
| **Inbox** | Seed/upload stub replies → **Parse** / **Parse all** → quotations + questionnaire answers | Document Parser |
| **Compare** | Side-by-side INR matrix, coverage, knockout pass/fail badges; only qualified vendors are award-eligible | Normalizer + qualification |
| **Ask** | Persistent live chat with the Analyst (history kept) | Analyst |
| **Award** | Per-line dropdown (qualified vendors with a price) → save → summary / print | Analyst `validate_award` / `suggest_split_award` |

Next step unlocks only when the previous stage is complete (Back always allowed).
An optional **auto-run demo (e2e)** link on the home page still exists for one-shot demos.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY
uvicorn app_crew:app --port 8518 --reload
```

Open http://127.0.0.1:8518 — start a draft and walk the wizard.

Without `ANTHROPIC_API_KEY`, structured JSON/CSV parse + normalize + offline analyst
still work once an RFx exists; drafting and unstructured parse need the key.

## Deploy

`vercel.json` targets **`app_crew.py`**. Import this repo into a **new** Vercel
project (do not reuse kill-the-quote-spreadsheet). Set `ANTHROPIC_API_KEY`.
Verify `/healthz` returns `"app": "rfx-crew"`. See `DEPLOY.md`.

## Layout

```
agents/                 Five crew agents + qualification.py
orchestrator/           Wizard-aware pipeline + persistence
shared_models.py        Pydantic contracts (incl. awards / inbox / knockouts)
app_crew.py             FastAPI wizard routes
templates/crew/         Wizard UI (wizard.html)
data/vendor_responses/  Sample multi-format vendor replies
data/inbox/             Stub inbound mail (per RFx)
data/outbox/            Stub dispatch emails
data/store/             Session snapshots (gitignored)
```

## Deliberately not built

Real SMTP/IMAP, vendor portal, ERP hand-off, multi-user auth. Arithmetic and
ranking stay in code; the Analyst explains over computed tables.

See `DECISIONS.md` and `DEMO_SCRIPT.md`.

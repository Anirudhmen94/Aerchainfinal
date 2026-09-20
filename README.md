# Aerchainfinal — RFx Crew (5 agents)

Evidence-minded sourcing prototype for corrugated packaging. A buyer describes a
requirement; a **crew of five agents** drafts the RFx, dispatches stub vendor emails,
parses whatever formats come back, normalises currency/UOM into one comparison matrix,
and answers natural-language questions toward an award.

Repository: https://github.com/Anirudhmen94/Aerchainfinal

This is a **new shareable deployment** of the crew architecture (entrypoint
`app_crew:app`). It is **not** the older kill-the-quote-spreadsheet Vercel project.

## 5-agent architecture

| # | Agent | Module | Responsibility |
|---|---|---|---|
| 1 | **RFx Drafter** | `agents/rfx_drafter.py` | Brief → structured `RFx` (scope, ~30 lines, terms, questionnaire, vendors) |
| 2 | **Vendor Dispatcher** | `agents/vendor_dispatcher.py` | Cover emails written to `data/outbox/` (SMTP stubbed) |
| 3 | **Document Parser** | `agents/document_parser.py` | Vendor files → `ExtractedQuote` (JSON/CSV deterministic; text/PDF/image via Haiku) |
| 4 | **Normalizer** | `agents/normalizer.py` | Map to RFx lines; USD→INR; UOM → INR/piece; flag gaps |
| 5 | **Analyst** | `agents/analyst.py` | NL questions over `ComparisonTable` (deterministic helpers + optional Claude) |

**Orchestrator:** `orchestrator/pipeline.py` runs Drafter → Dispatcher → Parser →
Normalizer in sequence and keeps an Analyst chat session. Shared contracts live in
`shared_models.py`.

```
Buyer brief
    │
    ▼
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────┐   ┌─────────┐
│ RFx Drafter │──▶│  Dispatcher  │──▶│ Doc Parser   │──▶│ Normalizer │──▶│ Analyst │
└─────────────┘   └──────────────┘   └──────────────┘   └────────────┘   └─────────┘
                         │                  ▲
                         ▼                  │
                   data/outbox/      data/vendor_responses/
```

The legacy `core/` + `app.py` monolith remains in the tree for reference; the crew
path does not depend on it.

## Run locally

Python 3.11+ recommended.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY for LLM drafting / unstructured parse
uvicorn app_crew:app --port 8518 --reload
```

Open http://127.0.0.1:8518

UI flow:

1. Enter brief → **Draft RFx**
2. **Dispatch** stub emails
3. **Ingest** sample vendor files (or upload)
4. **Normalize** into the comparison matrix
5. **Ask** NL questions (“cheapest per line”, “vendor totals”, “where are the gaps?”)

Without `ANTHROPIC_API_KEY`, JSON/CSV sample ingest + normalize + offline analyst still
work once an RFx exists; drafting and unstructured parse need the key.

## Deploy (new Vercel URL)

`vercel.json` targets **`app_crew.py`** (not the old `app.py` kill-the-quote project).

1. Import https://github.com/Anirudhmen94/Aerchainfinal into a **new** Vercel project
   (do not reuse kill-the-quote-spreadsheet).
2. Framework: Other / Python. Entrypoint `app_crew.py` → variable `app`.
3. Env: `ANTHROPIC_API_KEY`, optional `ANTHROPIC_HAIKU_MODEL` / `ANTHROPIC_SONNET_MODEL`.
4. Deploy. Verify `https://<new-app>.vercel.app/healthz` returns `"app": "rfx-crew"`.

See `DEPLOY.md`. Function timeout is 300s.

## Layout

```
agents/                 Five crew agents
orchestrator/           Sequential pipeline + session snapshot
shared_models.py        Pydantic contracts
app_crew.py             FastAPI + Jinja/HTMX buyer UI
templates/crew/         Crew UI
data/vendor_responses/  Sample multi-format vendor replies
data/outbox/            Stub dispatch emails
data/store/             Pipeline session snapshots (gitignored)
DECISIONS.md            Product/architecture choices for THIS crew
DEMO_SCRIPT.md          Walkthrough for THIS crew
vercel.json             New deploy config → app_crew.py
```

## Deliberately not built

Real SMTP/IMAP, vendor portal, ERP hand-off, multi-user auth, and hardcoded demo
answers. Arithmetic and ranking stay in code; the Analyst explains over computed tables.

See `DECISIONS.md` and `DEMO_SCRIPT.md`.

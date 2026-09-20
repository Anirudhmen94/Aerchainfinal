# Aerchainfinal — RFx Crew (5 agents)

Evidence-minded sourcing prototype for corrugated packaging. A buyer describes a requirement; a **crew of five agents** drafts the RFx, dispatches stub vendor emails, parses whatever formats come back, normalises currency/UOM into one comparison matrix, and answers natural-language questions toward an award.

Repository: https://github.com/Anirudhmen94/Aerchainfinal

This is a **new shareable deployment** of the crew architecture (entrypoint `app_crew:app`). It is not the older kill-the-quote-spreadsheet Vercel project.

## 5-agent architecture

| # | Agent | Module | Responsibility |
|---|---|---|---|
| 1 | **RFx Drafter** | `agents/rfx_drafter.py` | Brief → structured `RFx` (scope, lines, terms, questionnaire, vendors) |
| 2 | **Vendor Dispatcher** | `agents/vendor_dispatcher.py` | Package cover emails; write to `data/outbox/` (SMTP stubbed) |
| 3 | **Document Parser** | `agents/document_parser.py` | Read vendor files (json/csv/txt; stubs for pdf/docx/image) → `ExtractedQuote` |
| 4 | **Normalizer** | `agents/normalizer.py` | Map to RFx lines; USD→INR; per-100 / per-1000 / bundle → INR/piece; flag gaps |
| 5 | **Analyst** | `agents/analyst.py` | NL questions over `ComparisonTable` via deterministic helpers (LLM tool-calling lands here) |

**Orchestrator:** `orchestrator/pipeline.py` runs Drafter → Dispatcher → Parser → Normalizer in sequence and keeps an Analyst chat session. Shared contracts live in `shared_models.py`.

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

The legacy `core/` + `app.py` monolith remains in the tree for reference; the crew path does not depend on it.

## Run locally

Python 3.11+ recommended.

```bash
pip install -r requirements.txt
cp .env.example .env   # set ANTHROPIC_API_KEY when LLM agents are wired
uvicorn app_crew:app --port 8518 --reload
```

Open http://127.0.0.1:8518

Flow in the UI:

1. Enter brief → **Draft RFx**
2. **Dispatch** stub emails
3. **Ingest** sample vendor files (or upload)
4. **Normalize** into the comparison matrix
5. **Ask** NL questions (“cheapest per line”, “vendor totals”, “where are the gaps?”)

CLI one-shot (no UI):

```bash
python -c "from orchestrator.pipeline import run_pipeline; import json; print(json.dumps(run_pipeline(open('/dev/stdin').read()), indent=2))" <<'EOF'
Corrugated packaging for a snacks plant in Chakan, ~30 SKUs, INR delivered.

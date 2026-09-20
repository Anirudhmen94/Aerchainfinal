# Decisions — RFx Crew architecture

**One page for the reviewer driving the live demo of this crew build.**

## The bet

Trust beats format coverage. A buyer with crores on the line will only leave the
spreadsheet when every number on screen is either (a) traced to a vendor file or
(b) explicitly flagged. The crew architecture makes that rule enforceable: each agent
has one job and one output contract in `shared_models.py`.

## Why five agents (not a monolith)

| Agent | Why it exists as its own unit |
|---|---|
| **Drafter** | Conversational / generative. Failure mode is a bad RFx, not a bad price. |
| **Dispatcher** | Plumbing. SMTP stubbed; outbox artefacts keep “what did we ask?” auditable. |
| **Parser** | Format chaos. Deterministic JSON/CSV; LLM/vision only for unstructured files. |
| **Normalizer** | Pure transformation: FX, UOM, line mapping, gap flags. No prose. |
| **Analyst** | Language over a frozen matrix. Calls ranking helpers; does not redo FX math. |

A sequential orchestrator (`orchestrator/pipeline.py`) is enough — no CrewAI/LangChain
dependency. Each agent stays a plain Python module.

## Choices with trade-offs

- **FastAPI + Jinja/HTMX, not Streamlit.** Fits Vercel’s request model and a permanent
  public URL; evidence/matrix UI is ordinary HTML partials.
- **New Vercel project / URL.** Entrypoint `app_crew.py` is separate from any prior
  kill-the-quote-spreadsheet deployment so reviewers are not looking at a stale build.
- **Shared Pydantic contracts first.** Agents import `shared_models` only.
- **Stub SMTP, real outbox files.** Transport stubbed; every message under `data/outbox/`.
- **Normalizer flags over silent fixes.** USD→INR uses a dated demo rate; UOM conversions
  are explicit; missing / uncertain / uom_mismatch stay first-class statuses.
- **Analyst helpers are deterministic.** Cheapest-per-line and totals are Python. When
  Claude is available it narrates over precomputed tables.
- **Sample vendor pack in-repo.** Ugly-edge samples (JSON per-100, CSV with gaps,
  USD/1000 text, bundle rate card, per-kg email) so ingest demos without live mail.

## Deliberately left out

Real email delivery/ingestion, vendor logins, ERP/payment, multi-user ACL, production
queues, private blob storage, and hardcoded answers to demo questions.

## Where the interesting problem is

Not “can the model read the file?” — it can. The hard part is **row matching under
ambiguity** and **decisions under partial data** (“same as last year”, 27/30 lines,
incomplete knockout questionnaire). The crew surfaces those as flags and caveats.
Next build: a vendor confirm-mappings loop that closes ambiguity without retyping.

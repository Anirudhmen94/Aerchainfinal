# Product brief — Kill the Quote Spreadsheet (RFx Crew)

**Prototype:** https://kill-the-quote-spreadsheet-lac.vercel.app/  
**Repo:** https://github.com/Anirudhmen94/Aerchainfinal · `app_crew:app`  
**Category demo:** Corrugated packaging · Chakan (Pune) snacks plant · **5 vendors · 30 line items · 8 questionnaire items · FX 83.50 INR/USD**

---

## 1. Problem statement

Category buyers still spend days turning vendor chaos into a spreadsheet. They draft an RFx, email five suppliers, and get back whatever the supplier prefers: an Excel that ignores the template, a PDF with the discount in a footnote, a Word paragraph of commercials, a phone photo of a rate card, or a one-line email (“₹42/kg for the 5-ply… rest same as last year, freight extra”). Retyping takes three days. One VP question — *cheapest per line, only among vendors who cleared quality* — takes a fourth.

**We haven’t accepted that as the cost of doing business.** This prototype drafts the RFx with an AI co-pilot, accepts replies in any shape, normalizes them into one INR side-by-side matrix with questionnaire answers alongside the numbers, and lets the buyer interrogate the result in plain language through to a defensible award — with uncertainty and evidence treated as first-class product, not footnotes.

---

## 2. User groups

| Persona | What they need from this system |
|---|---|
| **Category buyer** (primary) | Draft fast, see every vendor on one screen, trust every cell, ask “what if we split?”, freeze an award they can defend. |
| **Category / sourcing manager** | Visibility when a buyer wants a Fail/incomplete vendor on a line — stub **manager notify** in Outbox (no Approve panel on Award); Audit trail of freezes and overrides. |
| **Vendor** (indirect) | Reply however they like; nobody is forced into a portal template. Award notices (stub) go out only to confirmed winners. |
| **Reviewer / demo driver** | Public durable URL, free tabs, seeded ugly-edge dataset, no local setup required for the happy path. |

---

## 3. Features

Free tabbed workspace — **Draft | Outbox | Inbox | Compare | Ask | Award | Audit** (no unlock gates; Home has **no E2E button**).

| Tab | Capability |
|---|---|
| **Draft** | Plain-language brief → generate ~30 lines + 8-question quality questionnaire (ISO / FSC / food-contact knockouts) + 5 Indian vendors; edit title/scope/terms/lines. |
| **Outbox** | Cover-email previews → stub **Send to vendors** → `data/outbox/`. Later: award notices + **manager notify** stubs (filter: RFQ / Award notice / Manager). |
| **Inbox** | Seed/upload the five ugly formats → Parse / Parse all → quotations + questionnaire answers; cards stay **quote-aligned** with Compare. |
| **Compare** | Side-by-side INR matrix; cell statuses `ok\|converted\|uncertain\|uom_mismatch\|missing`; knockout **Q / XQ** badges; **source-of-truth** evidence drawer (snippet + original file/media). |
| **Ask** | Persistent live chat; human tone (senior buyer briefing a colleague); premade chips incl. VP-defend; free-ask tool loop over the frozen matrix. |
| **Award** | Per-line dropdowns (Pass + priced only); suggest cheapest split; partial/override **requests notify manager via Outbox** (no Approve UI here); freeze; stub award notices; export Excel / CSV / Markdown. |
| **Audit** | Chronological `review_log`: awards saved, freezes, notices, cell overrides, partial requests. |

**Ugly-edge seed set** (`data/vendor_responses/`): V01 Excel per-100 · V02 PDF ~27/30 + footnote discount · V03 Word USD/1000 · V04 angled PNG per-box/bundle · V05 one-line email ₹/kg + “same as last year”.

**Trust mechanics:** evidence-linked cells · deterministic FX/UOM · knockout gating · Blob-backed cold-start reload of the full session snapshot.

---

## 4. Tech architecture — five bots + orchestrator sync

Agents are plain Python modules with **shared Pydantic contracts** in `shared_models.py`. There is **no CrewAI/LangChain dependency**. A sequential orchestrator (`orchestrator/pipeline.py` → `RFxPipeline`) owns the session, calls each agent’s public API, and **persists a JSON snapshot** after every step (local `data/store/` or `/tmp` on Vercel, dual-written to **Vercel Blob** when `BLOB_READ_WRITE_TOKEN` is set).

```
Buyer (FastAPI + Jinja/HTMX tabs)
        │
        ▼
┌─────────────────── RFxPipeline (orchestrator) ───────────────────┐
│  draft → dispatch → parse → normalize → ask → award → audit     │
│  snapshot JSON  ──►  STORE_DIR  +  core.storage (Blob cold-start)│
└────────┬──────────┬──────────┬──────────┬──────────┬─────────────┘
         │          │          │          │          │
   RFx Drafter  Vendor     Document   Quote       RFQ Analyst
                Dispatcher Parser     Normalizer  (+ qualification)
         │          │          │          │          │
         ▼          ▼          ▼          ▼          ▼
      RFx model  outbox/*.txt Extracted  Comparison  chat + awards
                 + manager     Quote[]   Table       + review_log
                 stubs                   (INR cells)
```

**How they hand off / sync**

| Handoff | Mechanism |
|---|---|
| Drafter → rest | Emits `RFx` (lines, questionnaire, vendors) into pipeline state. |
| Dispatcher → Outbox | Writes stub cover emails under `data/outbox/`; `dispatch_log` on the snapshot. |
| Parser → Normalizer | `ExtractedQuote[]` (lines, questionnaire answers, confidence, `raw_evidence`). Companion `*.extract.json` keeps offline demos honest. |
| Normalizer → Compare/Ask/Award | Pure transform → `ComparisonTable` cells + `VendorQualification` / `award_eligible_vendors`. No prose. |
| Analyst → Ask/Award | Reads the **frozen** comparison; ranking/totals in code; language over tables; `validate_award` / `suggest_split_award`. |
| Award → Outbox / Audit | Manager notify + award notices as outbox stubs; events appended to `review_log`. |
| Cross-instance sync | `_persist()` / `load_pipeline()` — local JSON first, then Blob — so cold starts recover the same `rfx_id`. |

**UI ↔ agents:** `app_crew.py` routes map 1:1 to pipeline methods (`draft`, `dispatch`, `seed_inbox`/`parse_*`, `normalize`, `ask`, `save_awards`, freeze/notify). Tab key `send` is labelled **Outbox** in the UI.

---

## 5. Success metrics

Demo / take-home bar (what “working” means for a reviewer):

| Metric | Target / signal |
|---|---|
| **Ugly-edge coverage** | All 5 seeded formats parse into the matrix without forcing a template. |
| **Normalization honesty** | USD and non-piece UOMs show as `converted` (or `uom_mismatch` / `uncertain` when unsafe); partial coverage shows as `missing` (e.g. ~27/30). |
| **Trust surface** | Every priced cell has openable source-of-truth evidence; Audit shows freeze / override / notice events. |
| **Quality-gated award** | Fail/XQ vendors excluded from default shortlist; partial path creates manager Outbox stub (not silent eligibility). |
| **Analyst defensibility** | VP-defend / cheapest-qualified-split answers cite computed tables + caveats; free-ask is not hardcoded. |
| **Demo latency & durability** | Stub dispatch / structured parse near-instant; Blob cold-start reload of the same `rfx_id` succeeds (`/healthz` → `"app": "rfx-crew"`). |
| **Buyer time (product thesis)** | Collapse “retype week + VP question afternoon” into a single live workspace session for a 5×30 event. |

**Deliberately out of scope for v1 metrics:** real email delivery SLAs, vendor portal adoption, ERP post-award sync, multi-tenant ACL.

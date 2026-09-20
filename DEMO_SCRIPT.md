# Demo script — RFx Crew (wizard + ugly edges)

~10–12 minutes plus model latency. Stub dispatch / deterministic parse / normalize is near-instant.

## 0. Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # ANTHROPIC_API_KEY for live draft + vision/LLM parse
python data/fixtures/generate_ugly_edges.py   # refresh binary samples if needed
uvicorn app_crew:app --port 8518 --reload
```

Open http://127.0.0.1:8518 — header shows the live product wizard.
`/healthz` should report `"app": "rfx-crew"`.

## 1. Draft (1–2 min)

Home → start with the Chakan snacks / corrugated brief → **Generate line items**.
Board shows ~30 lines, knockout questionnaire, five vendors. Note `rfx_id`.

## 2. Send (1 min)

Cover email previews → **Send to vendors**. Outbox (`data/outbox/`) has one stub
email per vendor. Nothing hit SMTP.

## 3. Inbox — ugly edges (2–3 min)

**Seed sample replies** loads the five assignment formats from `data/vendor_responses/`:

| File | Ugly edge the buyer should notice |
|---|---|
| `V01_ignore_template.xlsx` | Weird columns; rates **per 100 pcs** (ignores piece template) |
| `V02_letterhead_footnote.pdf` | Letterhead PDF; **~27/30 lines**; **3.5% discount in footnote** |
| `V03_prose_commercials.docx` | Word prose; **USD / per 1000** (FX + UOM) |
| `V04_rate_card_photo.png` | Angled phone photo; **per-box / per-bundle** |
| `V05_oneline_email.txt` | `₹42/kg for the 5-ply, 38 for the 3-ply, rest same as last year, freight extra.` |

**Parse all**. Cards show format + confidence + questionnaire answers alongside numbers.

## 4. Compare (2–3 min)

**Build comparison**. Walk colored statuses:

- `converted` — USD→INR and/or per-100 / per-1000 / per-box math
- `missing` — lines the PDF/photo/email never quoted
- `uncertain` — “same as last year”, low confidence
- `uom_mismatch` — kg / box without safe piece conversion

Expand a cell for original currency/UOM + flags + evidence snippet.
Knockout **Q / XQ** badges gate award eligibility.

## 5. Ask (2–3 min)

Use the suggested chips, especially:

> **Give an award recommendation I can defend to a VP.**

Also: cheapest qualified split, gaps, FX/UOM conversions. Expand caveats under answers.

## 6. Award + export (1–2 min)

**Suggest cheapest split** → review dropdowns (qualified + priced only) → **Save**.
Export **Excel / CSV / Markdown** of the award decision (or Print).

## 7. Close (30 s)

`/crew/{id}/snapshot` JSON matches `shared_models`. Point at `DECISIONS.md`: why five
agents, why FastAPI, why a **new** Vercel URL for `app_crew.py`, how uncertainty is shown.

## If asked “is this hardcoded?”

Change the brief, re-draft, re-seed. Line IDs and matrix shift with the drafted RFx;
rankings recompute from cells — no canned award paragraph. Binary samples are generated
artifacts, not screenshots of a fake UI.

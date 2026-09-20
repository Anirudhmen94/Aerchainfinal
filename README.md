# Aerchainfinal — Kill the Quote Spreadsheet

An evidence-backed sourcing prototype for corrugated packaging. A buyer describes a requirement in plain language, the co-pilot drafts a full RFx (scope, 30 line items, commercial terms, quality questionnaire), five vendors reply in five different formats, the system reads every reply into one normalised side-by-side comparison with click-through evidence, and the buyer interrogates the result in natural language all the way to an exportable award recommendation.

Built for the Aerchain "Kill the Quote Spreadsheet" product assignment. Repository: https://github.com/Anirudhmen94/Aerchainfinal. The AI loops are real (Anthropic Claude); the plumbing (email delivery, vendor identity) is stubbed.

**Deploy:** connect this repo to Vercel (Python/FastAPI + Blob). After deploy, `/healthz?fingerprint=1` returns a SHA-1 per file so the live build is verifiable against this source. A prior public prototype lived at https://kill-the-quote-spreadsheet-anirudhmen94s-projects.vercel.app.

## What it does

| Step | What happens | AI or code? |
|---|---|---|
| Brief → RFx | Plain-language brief becomes a structured RFx with exactly 30 line items, terms and a knockout-flagged questionnaire. Nominal carton weight per line is computed from dimensions and GSM. | AI drafts; code numbers, weights |
| Send | Cover emails are generated and placed in an Outbox. Nothing is actually sent. | Code (stub) |
| Vendor replies | Five synthetic replies are generated **from the drafted RFx** (so they cannot be pre-tuned): a styled Excel that ignores the template and prices per 100; a letterhead PDF quoting 27 of 30 lines with the discount in a footnote; a Word doc quoting USD per 1,000 with commercials in prose; an angled phone photo of a rate card priced per bundle of 20; and a one-line email ("₹54/kg for the 5-ply, 51 for the 3-ply, rest same as last year, freight extra"). You can also upload your own files. | Code |
| Read | Each file becomes *anchored text* (cell refs, page numbers, paragraph numbers, image line numbers). Photos are transcribed by the vision model. One structured extraction call per vendor maps rows to RFx lines and reports every price, term, questionnaire answer and certificate **with a verbatim evidence snippet**. Every snippet is then checked against the source text; anything not found verbatim is downgraded to *needs review*. | AI reads; code grounds |
| Compare | Per-100 / per-1000 / per-bundle / per-kg quotes are converted to INR per piece with the conversion shown in the cell; USD is converted at a fixed, dated rate. Cells are `ok`, `converted`, `needs review`, `unresolved`, `missing`, or `reviewed`. Click any cell for the evidence drawer: snippet, location, PDF page render with highlight, the photo, the conversion arithmetic, and an accept/override form that writes to an audit log. | Code |
| Ask | The analyst answers questions by calling deterministic engine functions (cheapest per line, like-for-like totals, single-vendor vs split awards, questionnaire gate, sensitivity, calculator). Every tool result is rendered as a table under the answer; caveats are generated from the data, not by the model. | AI chooses and explains; code computes |
| Award | Save any answer as the recommendation. Export an Excel workbook (summary, award by line, colour-coded comparison with conversion notes, flags, vendors, evidence index, review log), a Markdown memo, and a CSV. | Code |

## Run locally

Requires Python 3.11+ and an Anthropic API key.

```bash
pip install -r requirements.txt
cp .env.example .env            # then put your key in ANTHROPIC_API_KEY
uvicorn app:app --port 8517 --reload
```

Open http://127.0.0.1:8517. Without `BLOB_READ_WRITE_TOKEN` the app stores everything under `data/store/` (git-ignored).

Environment variables:

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | All model calls. Without it the UI shows a red banner and refuses to draft, extract or answer. It never fabricates output. |
| `ANTHROPIC_MODEL` | no | Defaults to `claude-sonnet-5`; falls back to `claude-sonnet-4-5` if the pinned model is unavailable. |
| `BLOB_READ_WRITE_TOKEN` | on Vercel | Vercel Blob store for files and state. Set automatically when a Blob store is connected to the project. |

Typical timings with Claude Sonnet: drafting the RFx 40-60 s; reading a vendor reply 40-130 s (the Word doc and the photo are slowest); an analyst answer 10-40 s.

## Deploy on Vercel

The app is a plain FastAPI application, which Vercel runs natively as a Python function (entrypoint `app.py`, `app` variable). `vercel.json` sets a 300 s function limit for the long extraction calls.

1. Create a Vercel project from this repository (or `vercel deploy` from the folder).
2. Storage → create a **Blob** store and connect it to the project. This injects `BLOB_READ_WRITE_TOKEN`.
3. Settings → Environment Variables → add `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`).
4. Redeploy. Check `https://<your-app>.vercel.app/healthz` shows `"ai": true` and `"storage": "vercel-blob"`.

State is written as immutable timestamped JSON versions and resolved through the Blob list API, which avoids CDN staleness on overwrite. Uploaded and generated vendor files are public-URL blobs under random paths; this is a demo with synthetic data, and a real deployment would use private blobs or signed URLs.

## Project layout

```
app.py                 FastAPI entrypoint: home, brief → draft, RFx page, send, outbox, AI log, local file serving
routes/inbox.py        simulate / upload / read (extract) vendor replies, anchored-text viewer, clarification emails
routes/compare.py      comparison grid, evidence drawer, PDF highlight renderer, buyer reviews, award page, exports
routes/ask.py          analyst chat, save recommendation
core/llm.py            Anthropic wrapper: forced-tool structured output with validation retry, tool-use loop, call log
core/models.py         Pydantic schemas the model must emit (RFx draft, extraction, transcription, email)
core/rfx_drafter.py    brief → RFx, nominal carton weight
core/vendor_sim.py     the five vendor personalities and their ugly file formats
core/ingest.py         xlsx / pdf / docx / image / email → anchored text
core/extractor.py      per-vendor structured extraction + evidence grounding
core/engine.py         deterministic normalisation, FX, coverage, cheapest-per-line, award scenarios, sensitivity, caveats
core/analyst.py        tool definitions, executor, calculator, table/chart rendering of tool results
core/export.py         Excel workbook, Markdown memo, CSV
core/clarify.py        open-point detection + AI-drafted clarification email (stub-sent)
core/storage.py        Vercel Blob / local filesystem backends, versioned state
templates/             Jinja2 + Tailwind (CDN) + HTMX (CDN) + Chart.js (CDN)
```

See `DECISIONS.md` for the one-page note on what was decided and deliberately left out, and `DEMO_SCRIPT.md` for a suggested walkthrough and questions worth asking.

## Deliberately not built

Real email delivery (SMTP is stubbed into an Outbox), vendor logins or a vendor portal, ERP or payment integration, multi-user access control, and any hardcoded answers to demo questions. Every number on screen is computed from what was extracted from the files in the inbox.

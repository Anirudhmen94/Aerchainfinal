# Decisions: what I built, what I left out, and where the real problem is

**One page. Written for the reviewer who will drive the live demo.**

## The bet

The spreadsheet does not die because extraction gets good enough. It dies when a buyer with ₹4 crore on the line trusts the screen more than their own retyping. So I optimised for *trust*, not for coverage of formats. Three rules follow from that, and every screen enforces them:

1. **No value without evidence.** The model may only report a price, term or answer if it can quote the verbatim text it came from and where (cell, page, paragraph, image line). The app then checks the quote against the source; if it is not there, the value is downgraded to *needs review*. The buyer can click any cell and see the cell, the highlighted PDF region, or the photo.
2. **The model never does arithmetic.** Unit conversion (per 100, per 1,000, per bundle of 20, per kg via a disclosed nominal weight), FX (fixed, dated rate), coverage, rankings, award totals, sensitivity: all in pandas/plain Python. The analyst is a tool-caller; every table under an answer is a raw engine result, and it has a `calculate` tool so even differences and percentages are computed, not recalled.
3. **Uncertainty is a first-class state, not a footnote.** Cells are `ok`, `converted`, `needs review`, `unresolved` ("same as last year"), `missing`, or `reviewed`. Anything not `ok`/`converted`/`reviewed` is excluded from every total unless the buyer explicitly asks to include it, and every answer ends with machine-generated caveats listing exactly what was excluded and why.

## Choices with no right answer, and why I went the way I did

- **Vendor files are generated from the drafted RFx, not pre-canned.** The RFx is genuinely AI-drafted each run, so a static dataset would not match. Generating the five replies in code from whatever the RFx contains also means extraction cannot be tuned to a file. The personalities are fixed (the Excel that ignores the template, the footnote discount, the USD-per-1,000 Word doc, the angled per-bundle photo, the one-line email), the numbers are not.
- **Per-kg quotes are converted, but loudly.** The email vendor quotes ₹/kg. I convert using an RSC blank-weight formula from the RFx dimensions and GSM, show the formula in the cell, and mark the cell `converted`. The alternative (refuse to compare) would hide the cheapest vendor. The alternative to that (silently convert) would hide the assumption. "Rest same as last year" is `unresolved`: we do not have last year, so we do not invent it.
- **Questionnaire gate is strict.** A vendor is *cleared* only if every knockout question has a clear pass. Unanswered is *incomplete*, not failed, and incomplete vendors are excluded from "cleared-only" awards but shown everywhere else. The photo vendor answered nothing; the analyst says so rather than guessing.
- **Freight and conditional discounts are never auto-applied.** They are extracted, flagged on the vendor header, listed in caveats, and applied only when the buyer asks ("apply conditional discounts"). Applying a 5%-above-₹25-lakh footnote automatically is exactly how spreadsheets lie.
- **Buyer overrides are allowed and logged.** A buyer who called the vendor can accept or override a flagged cell with a mandatory note. It shows as `reviewed`, in a different colour, and in the export's review log. Trust includes being able to see where a human intervened.
- **FastAPI, not Streamlit; Vercel, not Cloudflare.** The brief suggested Streamlit. I switched so the demo could be a permanent public URL on a free plan: Streamlit's persistent websocket server does not fit Vercel's request model, and Cloudflare cannot host it at all without a paid container plan. Server-rendered HTML with HTMX also made the evidence drawer and per-cell interactions cheap.
- **Claude Sonnet for everything, one wrapper.** Vision handles the photo (no Tesseract to deploy), forced tool calls give strict JSON, tool use gives the analyst. Every call is logged (purpose, tokens, latency) on an "AI call log" page so a reviewer can see there are exactly N calls and what each was for.

## What I deliberately left out

Real email (stubbed to an Outbox), vendor portal/logins, ERP hand-off, payments, multi-user roles, retries/queues for long extractions (each vendor is read in one request; a production system would queue), private file storage (synthetic data, public random-path blobs), and any hardcoding of demo answers. I also did not build a "template" for vendors: the whole point is that they never follow it.

## Where the interesting problem actually is

Extraction is now a commodity; a good model reads the angled photo. The hard part is **row matching under ambiguity and the buyer's decision under partial data**. Two RFx lines share a size and differ only in print; a vendor row omits the print. Was it a quote for line 1, line 2, or both? The system's honest answer is "candidates: 1, 2; needs review", and the product question is how much of that ambiguity a buyer will tolerate before they reopen Excel. My answer here: show the ambiguity, make resolving it one click with an audit trail, and draft the clarification email for them. The next thing I would build is not a better parser; it is a vendor-side "confirm these 4 mappings" link that closes the loop without anyone retyping anything.

# Decisions: what I built, what I left out, and where the real problem is

**One page · for the reviewer driving the live demo**  
**Live:** https://kill-the-quote-spreadsheet-lac.vercel.app/  
**Code:** https://github.com/Anirudhmen94/Aerchainfinal · entrypoint `app_crew:app`

## The bet

The spreadsheet does not die when extraction gets good enough. It dies when a buyer with serious money on the line trusts the screen more than their own retyping. This prototype optimises for **trust**, not format coverage. Three rules sit under every screen:

1. **No value without evidence.** Prices and questionnaire answers must point at the cell, page, paragraph, or image line they came from. Compare opens a **source-of-truth** evidence drawer (snippet + original media). Ungrounded values become *uncertain* / *missing*, not silent numbers.
2. **The model never does the arithmetic.** FX (demo **USD→INR 83.50**), UOM → INR/piece, cheapest-per-line, gated splits, and caveats run in Python. Ask chooses which helpers to call and explains the tables; the tables are the source of truth.
3. **Uncertainty is first-class.** Cells are `ok` · `converted` · `uncertain` · `uom_mismatch` · `missing`. Knockout-fail vendors get an **XQ** badge, are dimmed, and stay off the award shortlist unless the buyer opens a partial/override path.

## Choices with no right answer (and why)

- **Category = corrugated packaging (Chakan snacks plant).** Forces real UOM pain (per kg, per 100, per 1000, per box/bundle, USD). Demo size: **5 vendors, 30 lines, 8 questionnaire items** (ISO / FSC / food-contact knockouts among them).
- **Free tabs, not a locked wizard.** Draft | **Outbox** | Inbox | Compare | Ask | Award | **Audit** — always open; empty panels say what’s missing. Home is draft-only (**no E2E auto-run button**).
- **Five ugly replies, not a portal.** Excel ignoring the template, PDF with footnote discount (~27/30 lines), Word prose in USD/1000, angled phone photo, one-line email (“₹42/kg… same as last year, freight extra”). Inbox cards stay **quote-aligned** after parse.
- **Outbox is the outbound ledger.** Stub RFQ covers, **manager notify** stubs for partial/override (**no Approve panel on Award**), and award notices all land in `data/outbox/` with type filters. SMTP is fake; the artefacts are real.
- **Ask speaks human.** Analyst tone: senior buyer briefing a colleague — calm, direct — over a frozen matrix; VP-defend chip + free-ask tool loop; no hardcoded free-ask answers.
- **Audit + Blob cold-start.** Award save / freeze / notices / cell overrides append `review_log` on **Audit**. Snapshots dual-write local store + **Vercel Blob** so a cold instance can reload the event.
- **Stub plumbing; keep AI loops real.** Draft + unstructured parse need `ANTHROPIC_API_KEY`; structured parse / normalize / offline analyst still work once an RFx exists.

## Deliberately left out

Real SMTP/IMAP · vendor portal · ERP / payments · multi-user RBAC · production queues · Approve/Reject UI on Award (manager notified via Outbox stub only) · hardcoded free-ask answers · auto-applying footnote discounts into official award totals · Home one-click E2E.

## Where the interesting problem actually is

Extraction is increasingly a commodity. The hard product problem is **row matching under ambiguity** and **decisions under partial data** — “same as last year”, 27/30 lines, incomplete knockouts. The honest UI answer is flags + buyer override with an audit trail. Next build: a vendor “confirm these mappings” link that closes the loop without anyone retyping into Excel.

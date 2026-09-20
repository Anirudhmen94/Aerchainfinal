# Decisions: what I built, what I left out, and where the real problem is

**One page · for the reviewer driving the live demo**  
**Live:** https://kill-the-quote-spreadsheet-lac.vercel.app/  
**Code:** https://github.com/Anirudhmen94/Aerchainfinal · entrypoint `app_crew:app`

## The bet

The spreadsheet does not die when extraction gets good enough. It dies when a buyer with serious money on the line trusts the screen more than their own retyping. This prototype optimises for **trust**, not format coverage. Three rules:

1. **No value without evidence.** Prices and questionnaire answers must point at source. Compare opens a **source-of-truth** evidence drawer (snippet + original media). Ungrounded values become *uncertain* / *missing*, not silent numbers.
2. **The model never does the arithmetic.** FX (demo **USD→INR 83.50**), UOM → INR/piece, cheapest-per-line, gated splits, and caveats run in Python. Ask calls helpers and explains; the tables are the source of truth.
3. **Uncertainty is first-class.** Cells are `ok` · `converted` · `uncertain` · `uom_mismatch` · `missing`. Knockout-fail vendors get **XQ**, are dimmed, and stay off the award shortlist unless the buyer opens a partial/override path.

## Choices with no right answer (and why)

- **Corrugated packaging (Chakan snacks plant).** Real UOM pain (per kg / 100 / 1000 / box / USD). Demo: **5 vendors, 30 lines, 8 questionnaire items** (ISO / FSC / food-contact knockouts).
- **Free tabs, not a locked wizard.** Draft | **Outbox** | Inbox | Compare | Ask | Award | **Audit**. Home is draft-only (**no E2E button**).
- **Five ugly replies, not a portal.** Excel-off-template, PDF footnote discount (~27/30), Word USD/1000, angled photo, one-line email. Inbox stays **quote-aligned**.
- **Outbox = outbound ledger.** Stub RFQ covers, **manager notify** for partial/override, award notices → `data/outbox/`. SMTP fake; artefacts real.
- **Ask speaks human.** Senior-buyer tone over the normalized matrix; VP-defend + free-ask tool loop; no hardcoded answers.
- **Audit + Blob cold-start.** Saves, notices, overrides append `review_log`. Snapshots dual-write local store + **Vercel Blob** for cold reload.
- **Stub plumbing; AI loops real.** Draft + unstructured parse need `ANTHROPIC_API_KEY`; structured parse / normalize / offline analyst work once an RFx exists.

## What you deliberately left out

- **No real SMTP/IMAP.** Outbound is stub **Outbox files** only (`data/outbox/`). Proves artefacts and filters without pretending mail infrastructure is the product.
- **No Freeze / Unfreeze ceremony on Award.** Removed on purpose — award save + Audit log is enough for a demo; a freeze ritual added ceremony without trust.
- **No vendor portal / forced template.** Vendors reply however they like; the system absorbs mess instead of pushing compliance UI onto suppliers.
- **Manager notify-only — no Approve/Reject UI on Award.** Partial/override paths stub a manager notice in Outbox; the buyer owns the screen, not a fake approval workflow.
- **No ERP / payments.** Scope stops at a defensible award decision, not P2P settlement.
- **No multi-user RBAC.** Single-buyer demo; AuthN/AuthZ would dilute the ugly-edge story.
- **No hardcoded Ask answers.** Free-ask must run on extracted data; canned replies would fail “don’t fake the reasoning.”
- **No auto-applying footnote discounts into award totals.** Discounts stay visible as evidence/caveats; silent math into official totals would break trust.
- **No Home one-click E2E.** Tabs stay free; the reviewer drives the real path, not a hidden autopilot button.

## Where the interesting problem actually is

Extraction is increasingly a commodity. The hard product problem is **row matching under ambiguity** and **decisions under partial data** — “same as last year”, 27/30 lines, incomplete knockouts. Honest UI: flags + buyer override with an audit trail. Next: a vendor “confirm these mappings” link that closes the loop without retyping into Excel.

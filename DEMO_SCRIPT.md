# Demo script and questions worth asking

Total running time about 12 minutes, plus model latency. Have one sourcing event already extracted as a fallback in case a live extraction is slow.

## 1. The brief (1 min)

Home page. Read the pre-filled brief aloud (snacks plant in Chakan, ~30 SKUs, 3/5/7-ply, ₹3.8 crore last year). Click **Draft the RFx**. While it drafts (40-60 s), say what will come back: scope, exactly 30 line items with sizes/GSM/print/annual quantities, INR-delivered terms, and a questionnaire with knockout questions.

## 2. The RFx (1 min)

Point out one line's nominal weight ("~317 g/pc"): computed from dimensions and GSM, not by the model, and it is what makes the per-kg email comparable later. Edit one quantity, hit Save, show it recomputes. Click **Send to 5 vendors**. Open the **Outbox** to show the stubbed emails.

## 3. The inbox (2-3 min)

Click **Simulate 5 vendor replies**. Open each file briefly:
- Sri Balaji: beautiful Excel, own SKU codes, priced **per 100**, two alternates, 3% discount in a notes row, compliance on a second sheet.
- Kraftline: PDF on letterhead, **27 of 30 lines**, sequential numbering that does not match the RFx, 5% discount and "freight extra" in 6.5-pt footnotes.
- PakAsia: Word doc, **USD per 1,000, CIF Nhava Sheva**, 3-ply items priced in a paragraph as a blended rate with two exceptions.
- Meghna: **angled phone photo** of a rate card, "Rate (Rs./Box)", the "per box of 20 nos" only in the small print.
- Ganesh: the email. "₹54/kg for the 5-ply, 51 for the 3-ply, rest same as last year, freight extra."

Click **Read all 5 pending**. Each takes 40-130 s. While they run, click **text** on the photo to show the vision transcription with its legibility score and caveats. When a card finishes, point at the "Evidence verified 49/49" line: every value's quote was found in the source.

## 4. The comparison (3 min)

Open **Compare**. Everything is INR per piece. Walk the legend, then click:
- A blue **converted** cell for Ganesh: shows `51/kg × nominal 316.8 g/pc → 16.16` with the RSC formula. The assumption is on screen.
- A blue cell for PakAsia: `614.5 per 1000 → ÷1000; USD→INR @ 83.5 (2026-09-15)`.
- A blue cell for Meghna: `330 per bundle of 20 → ÷20`, with the photo and the small print in context.
- A Kraftline cell: the PDF page renders with the price highlighted.
- An amber **needs review** cell (Sri Balaji alternate flute, or an ambiguous mapping): read the reason. Use the **Buyer review** form to accept it with a note; it turns purple and appears in the review log and export.
- A red **unresolved** Ganesh cell on a 7-ply line: "rest same as last year". We do not have last year, so it is not priced.
- Click a vendor header to open the questionnaire drawer. Meghna answered nothing: *incomplete*, not failed.

## 5. The conversation (4 min)

Open **Ask**. Suggested questions are on the right. Ask, in this order:

1. **"What if we split it, cheapest per line, but only among vendors who cleared the quality questionnaire?"**
   Expect: 3-vendor split vs 5-vendor split, the premium computed by the calculator tool, and a note that Ganesh is cheapest on most lines but incomplete. Expand **Data used** to show the raw engine tables, and **Tool trace** to show which functions ran.
2. **"Who quoted in USD, what rate did you use, and how does a 3% weaker rupee change the ranking?"**
   Expect: PakAsia named, FX table shown, `sensitivity` tool run on PakAsia at +3%, lines that change winner listed.
3. **"Which lines have no usable quote from anyone or only one usable quote, and what do I need to clarify?"**
   Then go to the inbox and click **Draft clarification** on Ganesh: the engine detects the open points, the model writes the email, it lands in the Outbox.
4. **"If we want at most two suppliers, which two, and what does it cost versus the full split?"**
   Expect: `award_scenario(strategy=split_max_n, max_vendors=2)`.
5. **"Give me an award recommendation I can defend to the VP, with the conditions under which it changes."**
   Click **Save as award recommendation**.

Questions that show the edges honestly:
- "Apply every vendor's conditional discount and re-rank." (They are never applied by default; the analyst will say what condition each depends on.)
- "Include the needs-review cells and tell me what changes." (Shows how much of the answer rests on flagged data.)
- "What did Kraftline not quote, and who is cheapest on those lines?"
- "Why is Sri Balaji marked freight-included and Kraftline freight-extra?" (Evidence for both terms.)

## 6. The export (1 min)

**Award** page: saved recommendation, cheapest-per-line table, data quality per vendor, review log. Click **Export award workbook** and open the Excel: the Comparison sheet is colour-coded with conversion notes as cell comments; the Evidence sheet lists 150 snippets with "found verbatim" yes/no. Open **AI call log** last: every model call for this event, with tokens and latency. Nothing else touched a model.

## If asked "how do I know you didn't hardcode this?"

Draft a new RFx with a different corrugated brief (the vendor simulator is corrugated-specific; MRO or IT hardware would need new vendor personalities), for example "pharma secondary packaging for a plant in Baddi, mostly 5-ply, export-grade". The line items, sizes and quantities change, the five generated files change with them, and the extraction runs live. Or upload your own file to any vendor.

# Ask / Analyst — recorded walkthrough script

**Goal:** Assignment deliverable — “a recorded walkthrough of the analyst conversation.”  
**Length:** ~3.5–5 minutes (plus model latency; pause recording during long waits if needed).  
**URL:** https://kill-the-quote-spreadsheet-lac.vercel.app/  
**Before you hit record:** Have one RFx already through **Inbox → Parse all → Compare** (matrix must exist). Stay on the **Ask** tab. Hard-refresh once.

---

## Shot list (one continuous take preferred)

| Time | On screen | You do |
|------|-----------|--------|
| 0:00–0:20 | Ask tab + chips | Cold open + setup line |
| 0:20–1:40 | VP chip → table + bars | Click **VP: split cheapest / qualified only** |
| 1:40–2:50 | Pass/Fail chip → chart | Click **Pass vs Fail coverage** |
| 2:50–4:10 | Defendable chip → prose | Click **Defendable award to VP** |
| 4:10–4:45 | Optional hard chip | One ugly-edge chip (USD or “same as last year”) |
| 4:45–5:00 | Chat thread | Close |

---

## Spoken script (say this)

### 1. Open (≈20s)

> “This is the Ask analyst on our Kill the Quote Spreadsheet prototype.  
> We’ve already drafted the Chakan corrugated RFx, stub-sent to five vendors, parsed the ugly replies, and built the comparison matrix.  
> I’m not clicking through spreadsheets — I’m asking in plain language over the normalised data.  
> Three questions: the VP split, who cleared quality, and a defendable award.”

*(Cursor over premade chips. Don’t read every chip.)*

---

### 2. VP question — money shot (≈70–90s)

**Click:** `VP: split cheapest / qualified only`

*(While it thinks: “Analyst is thinking…” — stay quiet or say “live Claude call over the matrix.”)*

When the answer lands, **scroll so the table and bar chart are visible**, then say:

> “This is the assignment’s VP question — split the award, cheapest per line, but only among vendors who cleared the quality questionnaire.  
> Under the prose you’ve got a deterministic table: line, Pass-only winner, rupees per piece, and the gap to the next Pass vendor.  
> The small bars are spend by awarded vendor on that split — Python math, not a hallucinated total.  
> Fail or incomplete vendors never win a line here.”

*(Hover 2–3 rows. Point at one runner-up gap.)*

---

### 3. Pass vs Fail coverage (≈60–70s)

**Click:** `Pass vs Fail coverage`

When the chart appears:

> “Same matrix, different question — who actually cleared the questionnaire.  
> Pass, Fail, Incomplete by vendor. This is the gate behind the split we just saw.  
> I don’t need to leave Ask and dig through Compare to explain knockouts to a VP.”

*(If a Fail bar is obvious, name that vendor once.)*

---

### 4. Defendable award (≈70–90s)

**Click:** `Defendable award to VP`

When prose returns:

> “Last ask: what would I actually take upstairs.  
> Who’s in or out on knockouts, whether we split or stay with one cleared supplier, rough totals, and the main risks — FX, partial coverage, uncertain cells like ‘same as last year.’  
> No canned paragraph — if we change the brief and re-parse, this recomputes.”

*(Don’t oversell. One short pause after the answer so the viewer can read.)*

---

### 5. Optional ugly-edge beat (≈30–40s) — only if you have time

**Click one of:**
- chip about USD / FX conversion, or  
- chip about incomplete lines / “same as last year”

> “Quick ugly edge: [USD vendor / incomplete quote]. The answer should flag converted or uncertain cells — that’s the trust surface, not a clean spreadsheet fantasy.”

---

### 6. Close (≈15s)

> “That’s the analyst loop: natural language in, real extracted data out — table, chart, and a defendable story.  
> Full flow continues on Award with the recommended split and stub notices. Thanks.”

**Stop recording.**

---

## Do / don’t

**Do**
- Use the live lac URL (or local :8518) with a **completed Compare**
- Wait for table/chart before talking over them
- Keep voice calm — “senior buyer,” not product-demo hype
- If latency >15s, pause recording, resume when the answer is ready

**Don’t**
- Start Ask before Parse + Compare (empty matrix = weak take)
- Click Award mid-recording (this video is Ask-only)
- Claim SMTP or live vendor email
- Scroll so fast the table/chart never fill the frame

---

## Backup if a chip fails

1. Refresh Ask once (session rehydrate).  
2. Re-open the same RFx from Home recent list.  
3. Type the VP question manually:

> What if we split the award, cheapest per line, but only among vendors who cleared the quality questionnaire?

Artifacts still attach on intent match even if LLM falls back to offline prose.

---

## Suggested filename

`aerchain-ask-analyst-walkthrough.mp4`  
Title card (optional, 3s): *Kill the Quote Spreadsheet — Analyst walkthrough*

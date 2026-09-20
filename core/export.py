"""Exports: award workbook (openpyxl), markdown memo, comparison CSV."""
from __future__ import annotations

import io
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import engine

HEAD = PatternFill("solid", fgColor="0F172A")
STATUS_FILL = {
    "ok": "DCFCE7",
    "converted": "DBEAFE",
    "reviewed": "E9D5FF",
    "needs_review": "FEF3C7",
    "unresolved": "FECACA",
    "missing": "F1F5F9",
    "not_extracted": "F1F5F9",
}


def _sheet_from_rows(wb: Workbook, title: str, columns: list[str], rows: list[list], widths: dict | None = None):
    ws = wb.create_sheet(title[:31])
    for c, h in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r, row in enumerate(rows, start=2):
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v)
    for i, col in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(col, max(12, min(60, len(str(col)) + 4)))
    ws.freeze_panes = "A2"
    return ws


def award_workbook(state: dict) -> bytes:
    cmp = engine.build_comparison(state)
    rec = state.get("recommendation") or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"Award recommendation: {cmp['title']}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"RFx {cmp['rfx_id']} · generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    ws["A4"] = "Recommendation (analyst answer saved by buyer)"
    ws["A4"].font = Font(bold=True)
    ws["A5"] = rec.get("answer") or "No recommendation saved yet. Ask the analyst and click 'Save as award recommendation'."
    ws["A5"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A5:H20")
    ws["A22"] = "Question asked"
    ws["A22"].font = Font(bold=True)
    ws["A23"] = rec.get("question", "")
    ws["A25"] = "Caveats"
    ws["A25"].font = Font(bold=True)
    for i, c in enumerate(rec.get("caveats") or engine.caveats_for(cmp, None, None, False), start=26):
        ws.cell(row=i, column=1, value=c)
    ws.column_dimensions["A"].width = 110

    # Award by line: cheapest per usable, unless the saved recommendation carried a split table
    cp = engine.cheapest_per_line(cmp, None, False, False)
    _sheet_from_rows(
        wb,
        "Award by line",
        ["Line", "SKU", "Description", "Annual qty", "Awarded to", "Unit INR", "Extended INR", "Runner-up", "Runner-up unit INR", "Gap %"],
        [[r["line_no"], ln["sku"], r["description"], r["annual_qty"], r["winner"], r["unit_inr"], r["extended_inr"], r["runner_up"], r["runner_up_unit_inr"], r.get("gap_pct")] for r, ln in zip(cp["rows"], cmp["lines"])],
        {"Description": 50, "Awarded to": 28, "Runner-up": 28},
    )
    ws2 = wb["Award by line"]
    ws2.cell(row=len(cp["rows"]) + 3, column=6, value="Total")
    ws2.cell(row=len(cp["rows"]) + 3, column=7, value=cp["total_extended_inr"]).font = Font(bold=True)
    if cp["uncovered_lines"]:
        ws2.cell(row=len(cp["rows"]) + 4, column=1, value=f"Lines without a usable quote: {cp['uncovered_lines']}")

    # Full comparison with status colouring
    cols = ["Line", "SKU", "Description", "Board", "Annual qty"] + [v["name"] for v in cmp["vendors"]]
    ws3 = _sheet_from_rows(wb, "Comparison", cols, [], {"Description": 50})
    for r, ln in enumerate(cmp["lines"], start=2):
        base = [ln["line_no"], ln["sku"], ln["description"], ln["board"], ln["annual_qty"]]
        for c, v in enumerate(base, start=1):
            ws3.cell(row=r, column=c, value=v)
        for i, v in enumerate(cmp["vendors"]):
            cell = ln["cells"][v["vendor_id"]]
            xc = ws3.cell(row=r, column=6 + i, value=cell.get("unit_inr") if cell.get("unit_inr") is not None else cell["status"])
            xc.fill = PatternFill("solid", fgColor=STATUS_FILL.get(cell["status"], "FFFFFF"))
            if cell.get("conversion") or cell.get("reason"):
                from openpyxl.comments import Comment

                xc.comment = Comment(((cell.get("conversion") or "") + "\n" + (cell.get("reason") or "")).strip()[:900], "engine")
    legend_row = len(cmp["lines"]) + 3
    ws3.cell(row=legend_row, column=1, value="Legend: green ok · blue converted · purple reviewed · amber needs review · red unresolved · grey missing. Hover a cell for conversion notes.")

    # Flags and caveats
    fl = engine.list_flags(cmp)
    _sheet_from_rows(wb, "Flags", ["Line", "Vendor", "Status", "Flags", "Reason", "Conversion", "Raw price", "Currency", "Basis", "Unit INR", "Confidence"], [[i["line_no"], i["vendor"], i["status"], ", ".join(i["flags"]), i["reason"], i["conversion"], i["raw_price"], i["currency"], i["basis"], i["unit_inr"], i["confidence"]] for i in fl["items"]], {"Reason": 60, "Conversion": 60, "Vendor": 28})

    # Vendor summary
    _sheet_from_rows(wb, "Vendors", ["Vendor", "Usable lines", "Needs review", "Unresolved", "Missing", "Questionnaire", "Knockouts open", "Knockouts failed", "Freight", "Payment", "Validity", "Discount", "Avg confidence"], [[v["name"], v["usable"], v["counts"].get("needs_review", 0), v["counts"].get("unresolved", 0), v["counts"].get("missing", 0), v["questionnaire"]["overall"], ", ".join(v["questionnaire"]["knockout_open"]), ", ".join(v["questionnaire"]["knockout_failed"]), v["commercial"].get("freight_text"), v["commercial"].get("payment"), v["commercial"].get("validity"), v["commercial"].get("discount_condition"), v["avg_confidence"]] for v in cmp["vendors"]], {"Vendor": 28, "Freight": 40, "Payment": 30, "Discount": 50})

    # Evidence index
    ev_rows = []
    for ln in cmp["lines"]:
        for v in cmp["vendors"]:
            c = ln["cells"][v["vendor_id"]]
            ev = c.get("evidence")
            if ev:
                ev_rows.append([ln["line_no"], v["name"], c.get("raw_price"), c.get("currency"), c.get("basis"), ev.get("file_name"), ev.get("location"), ev.get("snippet"), "yes" if ev.get("verified") else "NO"])
    _sheet_from_rows(wb, "Evidence", ["Line", "Vendor", "Raw price", "Currency", "Basis", "File", "Location", "Verbatim snippet", "Verified in source"], ev_rows, {"Vendor": 28, "File": 34, "Location": 20, "Verbatim snippet": 70})

    # Reviews / overrides log
    _sheet_from_rows(wb, "Review log", ["When", "Vendor", "Line", "Action", "Value INR", "Note"], [[r.get("at"), r.get("vendor_name"), r["line_no"], r["action"], r.get("value_inr"), r.get("note")] for r in state.get("reviews", [])], {"Note": 60})

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def comparison_csv(state: dict) -> bytes:
    df = engine.to_dataframe(engine.build_comparison(state))
    return df.to_csv(index=False).encode("utf-8")


def award_memo_md(state: dict) -> str:
    cmp = engine.build_comparison(state)
    rec = state.get("recommendation") or {}
    cp = engine.cheapest_per_line(cmp, None, False, False)
    out = [f"# Award recommendation: {cmp['title']}", "", f"RFx `{cmp['rfx_id']}` · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    out += ["## Recommendation", "", rec.get("answer") or "_No recommendation saved yet._", ""]
    if rec.get("question"):
        out += [f"_Question asked:_ {rec['question']}", ""]
    out += ["## Cheapest-per-line split (usable quotes only)", "", f"Total extended annual value: **{engine.fmt_inr(cp['total_extended_inr'])}**", ""]
    for name, s in cp["share_by_vendor"].items():
        out.append(f"- {name}: {s['lines']} lines, {engine.fmt_inr(s['extended_inr'])}")
    if cp["uncovered_lines"]:
        out.append(f"- Lines with no usable quote: {cp['uncovered_lines']}")
    out += ["", "## Vendor status", ""]
    for v in cmp["vendors"]:
        out.append(f"- **{v['name']}**: usable {v['usable']}/{cmp['line_count']}, needs review {v['counts'].get('needs_review',0)}, unresolved {v['counts'].get('unresolved',0)}, missing {v['counts'].get('missing',0)}; questionnaire {v['questionnaire']['overall']}; freight {'extra' if v['commercial'].get('freight_extra') else 'as quoted'}")
    out += ["", "## Caveats", ""]
    for c in rec.get("caveats") or engine.caveats_for(cmp, None, None, False):
        out.append(f"- {c}")
    if state.get("reviews"):
        out += ["", "## Buyer review log", ""]
        for r in state["reviews"]:
            out.append(f"- {r.get('at','')[:16]} {r.get('vendor_name')} L{r['line_no']}: {r['action']}{' → ₹' + str(r['value_inr']) if r.get('value_inr') is not None else ''} — {r.get('note','')}")
    return "\n".join(out)

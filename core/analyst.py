"""Natural-language analyst over the comparison.

The model never does arithmetic. It decides *which* deterministic engine
functions to call and with what filters, then explains the results. The UI
renders every tool result as a table, so the numbers the buyer sees come from
code, and the prose is clearly the model's.
"""
from __future__ import annotations

from typing import Any

from . import engine, llm

SYSTEM = """You are the sourcing analyst for a category buyer comparing five suppliers' quotes for corrugated packaging.
You answer questions about the comparison strictly by calling the tools provided. Rules:

- NEVER do arithmetic, rank, or estimate yourself. Every number you state must come from a tool result in this conversation.
  For differences, premiums, percentages or sums across tool results, call `calculate`.
- Call tools first, then answer. Use several tools if the question needs them (e.g. cheapest_per_line AND questionnaire_overview).
- Respect the buyer's constraints literally: "only vendors who cleared the questionnaire" → require_cleared_questionnaire=true;
  "include flagged/uncertain values" → allow_needs_review=true; otherwise keep the defaults (flagged values excluded).
- Do NOT reproduce full tables in your answer; the interface shows every tool result you used as a table. Quote the key
  figures (totals, winners, counts) and explain what they mean.
- Be explicit about what was excluded and why (missing lines, unresolved lines, freight extra, conditional discounts,
  FX assumptions). Tool results include `caveats`; reflect the ones that matter for the question.
- If the buyer asks for a decision, end with a section titled "Recommendation" giving a clear, defensible call and
  the two or three conditions under which it would change. If data is too uncertain to decide, say so and name the
  exact clarifications needed from which vendor.
- Refer to vendors by name. Amounts are INR unless a tool says otherwise. Keep answers tight: short paragraphs, bullets where useful.
- If a question cannot be answered from the data (e.g. asks about something not in any quote), say so plainly.
"""

TOOLS: list[dict] = [
    {
        "name": "comparison_table",
        "description": "Normalised INR-per-piece prices for every line and vendor, with cell status. Optionally filter by vendors or line numbers.",
        "input_schema": {"type": "object", "properties": {"vendor_ids": {"type": "array", "items": {"type": "string"}, "description": "vendor ids (v1..v5) or names; omit for all"}, "line_nos": {"type": "array", "items": {"type": "integer"}}}},
    },
    {
        "name": "cheapest_per_line",
        "description": "For each RFx line, the cheapest usable quote among eligible vendors, the runner-up, the extended annual value, and the resulting share by vendor. This is the 'split award' view.",
        "input_schema": {"type": "object", "properties": {"vendor_ids": {"type": "array", "items": {"type": "string"}}, "require_cleared_questionnaire": {"type": "boolean", "default": False}, "allow_needs_review": {"type": "boolean", "default": False, "description": "include cells flagged needs_review (default excludes them)"}}},
    },
    {
        "name": "vendor_totals",
        "description": "Per-vendor extended totals: on the lines each vendor covered, and like-for-like on the lines all listed vendors covered. Optionally apply each vendor's conditional discount.",
        "input_schema": {"type": "object", "properties": {"vendor_ids": {"type": "array", "items": {"type": "string"}}, "allow_needs_review": {"type": "boolean", "default": False}, "apply_conditional_discounts": {"type": "boolean", "default": False}}},
    },
    {
        "name": "award_scenario",
        "description": "Build an award: 'split_cheapest' (cheapest per line), 'single_vendor' (rank vendors as sole supplier, showing uncovered lines), or 'split_max_n' (best split limited to max_vendors suppliers).",
        "input_schema": {"type": "object", "properties": {"strategy": {"type": "string", "enum": ["split_cheapest", "single_vendor", "split_max_n"]}, "vendor_ids": {"type": "array", "items": {"type": "string"}}, "require_cleared_questionnaire": {"type": "boolean", "default": False}, "allow_needs_review": {"type": "boolean", "default": False}, "max_vendors": {"type": "integer"}, "apply_conditional_discounts": {"type": "boolean", "default": False}}, "required": ["strategy"]},
    },
    {
        "name": "sensitivity",
        "description": "What-if: shift one vendor's prices by pct_change (e.g. 10 for +10%, -5 for -5%) and show how the cheapest-per-line award changes.",
        "input_schema": {"type": "object", "properties": {"vendor_id": {"type": "string"}, "pct_change": {"type": "number"}, "require_cleared_questionnaire": {"type": "boolean", "default": False}, "allow_needs_review": {"type": "boolean", "default": False}}, "required": ["vendor_id", "pct_change"]},
    },
    {
        "name": "list_flags",
        "description": "Every cell that is missing, unresolved, needs_review or converted, with the reason and conversion note. Use to explain data quality.",
        "input_schema": {"type": "object", "properties": {"vendor_ids": {"type": "array", "items": {"type": "string"}}, "statuses": {"type": "array", "items": {"type": "string", "enum": ["missing", "unresolved", "needs_review", "converted", "ok", "reviewed"]}}}},
    },
    {"name": "questionnaire_overview", "description": "Questionnaire clearance per vendor (cleared / incomplete / failed), knockout questions open or failed, and certificates attached vs merely claimed.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "commercial_terms", "description": "Payment, validity, GST, lead time, MOQ, freight and discount terms per vendor as extracted.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "fx_rates", "description": "The fixed FX table used for currency normalisation and its as-of date.", "input_schema": {"type": "object", "properties": {}}},
    {
        "name": "calculate",
        "description": "Deterministic calculator for any arithmetic you need on numbers returned by other tools (differences, percentages, sums). Give a plain expression using numbers, + - * / ( ) and percent(a, b) which returns a/b*100. Never do this arithmetic in your head.",
        "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}, "label": {"type": "string", "description": "what this number is, e.g. 'premium for cleared-only split'"}}, "required": ["expression"]},
    },
    {
        "name": "make_chart",
        "description": "Ask the interface to draw a chart from engine data (not from numbers you type). kinds: 'vendor_totals_common' (bar of like-for-like totals), 'split_share' (bar of extended value by winning vendor), 'line_prices' (grouped bar of per-piece prices for given line_nos, max 8), 'coverage' (stacked bar of cell statuses per vendor).",
        "input_schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["vendor_totals_common", "split_share", "line_prices", "coverage"]}, "line_nos": {"type": "array", "items": {"type": "integer"}}, "require_cleared_questionnaire": {"type": "boolean", "default": False}, "allow_needs_review": {"type": "boolean", "default": False}}, "required": ["kind"]},
    },
]


def _chart(cmp: dict, kind: str, line_nos=None, require_cleared=False, allow_nr=False) -> dict:
    if kind == "vendor_totals_common":
        vt = engine.vendor_totals(cmp, None, allow_nr)
        return {"chart": {"type": "bar", "title": f"Like-for-like total on {vt['common_line_count']} common lines (INR)", "labels": [v["vendor"] for v in vt["vendors"]], "datasets": [{"label": "INR", "data": [v["total_on_common_lines_inr"] for v in vt["vendors"]]}]}}
    if kind == "split_share":
        cp = engine.cheapest_per_line(cmp, None, require_cleared, allow_nr)
        return {"chart": {"type": "bar", "title": "Cheapest-per-line award: extended value by vendor (INR)", "labels": list(cp["share_by_vendor"].keys()), "datasets": [{"label": "INR", "data": [s["extended_inr"] for s in cp["share_by_vendor"].values()]}]}}
    if kind == "line_prices":
        lns = [ln for ln in cmp["lines"] if not line_nos or ln["line_no"] in line_nos][:8]
        return {"chart": {"type": "bar", "title": "Per-piece price by vendor (INR)", "labels": [f"L{ln['line_no']}" for ln in lns], "datasets": [{"label": v["name"], "data": [ln["cells"][v["vendor_id"]].get("unit_inr") for ln in lns]} for v in cmp["vendors"]]}}
    if kind == "coverage":
        keys = ["ok", "converted", "reviewed", "needs_review", "unresolved", "missing"]
        return {"chart": {"type": "bar", "stacked": True, "title": "Cell status per vendor", "labels": [v["name"] for v in cmp["vendors"]], "datasets": [{"label": k, "data": [v["counts"].get(k, 0) for v in cmp["vendors"]]} for k in keys]}}
    raise ValueError("unknown chart kind")


def safe_calculate(expression: str) -> float:
    """Evaluate arithmetic safely via the AST: numbers, + - * / ** unary minus, parentheses, percent(a, b)."""
    import ast
    import operator as op

    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Pow: op.pow, ast.USub: op.neg, ast.UAdd: op.pos}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "percent" and len(node.args) == 2:
            a, b = ev(node.args[0]), ev(node.args[1])
            return a / b * 100 if b else float("nan")
        raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")

    import re as _re

    # strip thousands separators (Western 1,234,567 and Indian 46,32,46,40) but keep argument commas
    cleaned = _re.sub(r"(?<=\d),(?=\d{2,3}(?!\d))", "", expression).replace("₹", "").replace("INR", "").replace("Rs.", "").strip()
    return ev(ast.parse(cleaned, mode="eval"))


def make_executor(cmp: dict):
    def execute(name: str, args: dict) -> Any:
        if name == "calculate":
            val = safe_calculate(args["expression"])
            return {"label": args.get("label", ""), "expression": args["expression"], "result": round(val, 4)}
        if name == "comparison_table":
            return engine.comparison_table(cmp, args.get("vendor_ids"), args.get("line_nos"))
        if name == "cheapest_per_line":
            return engine.cheapest_per_line(cmp, args.get("vendor_ids"), args.get("require_cleared_questionnaire", False), args.get("allow_needs_review", False))
        if name == "vendor_totals":
            return engine.vendor_totals(cmp, args.get("vendor_ids"), args.get("allow_needs_review", False), args.get("apply_conditional_discounts", False))
        if name == "award_scenario":
            return engine.award_scenario(cmp, args.get("strategy", "split_cheapest"), args.get("vendor_ids"), args.get("require_cleared_questionnaire", False), args.get("allow_needs_review", False), args.get("max_vendors"), args.get("apply_conditional_discounts", False))
        if name == "sensitivity":
            return engine.sensitivity(cmp, args["vendor_id"], float(args["pct_change"]), args.get("require_cleared_questionnaire", False), args.get("allow_needs_review", False))
        if name == "list_flags":
            return engine.list_flags(cmp, args.get("vendor_ids"), args.get("statuses"))
        if name == "questionnaire_overview":
            return engine.questionnaire_overview(cmp)
        if name == "commercial_terms":
            return engine.commercial_terms(cmp)
        if name == "fx_rates":
            return cmp["fx"]
        if name == "make_chart":
            return _chart(cmp, args["kind"], args.get("line_nos"), args.get("require_cleared_questionnaire", False), args.get("allow_needs_review", False))
        raise ValueError(f"unknown tool {name}")

    return execute


def _context_summary(cmp: dict) -> str:
    lines = [f"Sourcing event: {cmp['title']}. {cmp['line_count']} RFx lines. Vendors (id: name, usable lines, questionnaire, freight extra?):"]
    for v in cmp["vendors"]:
        lines.append(f"- {v['vendor_id']}: {v['name']} ({v['city']}) — usable {v['usable']}/{cmp['line_count']}, needs_review {v['counts'].get('needs_review',0)}, unresolved {v['counts'].get('unresolved',0)}, missing {v['counts'].get('missing',0)}; questionnaire {v['questionnaire']['overall']}; freight extra: {v['commercial'].get('freight_extra')}; currency hint: {v.get('currency_hint')}")
    lines.append("Line items: " + "; ".join(f"L{ln['line_no']} {ln['board']} {ln['description'][:40]}" for ln in cmp["lines"]))
    return "\n".join(lines)


def tables_from_trace(trace: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Turn tool outputs into renderable tables, charts and caveats."""
    tables, charts, caveats = [], [], []

    def add_table(title, rows, columns=None):
        if not rows:
            return
        cols = columns or list({k: None for r in rows for k in r.keys()}.keys())
        tables.append({"title": title, "columns": cols, "rows": [[r.get(c) for c in cols] for r in rows]})

    for t in trace:
        if not t.get("ok"):
            continue
        out = t["output"]
        name = t["tool"]
        args = ", ".join(f"{k}={v}" for k, v in (t.get("input") or {}).items())
        title = f"{name}({args})"
        if isinstance(out, dict):
            for c in out.get("caveats", []) or []:
                if c not in caveats:
                    caveats.append(c)
            if "chart" in out:
                charts.append(out["chart"])
            elif name == "comparison_table":
                add_table(title, out["rows"], out["columns"])
            elif "rows" in out and isinstance(out["rows"], list):
                add_table(title, out["rows"])
                if out.get("share_by_vendor"):
                    add_table(title + " · share by vendor", [{"vendor": k, **v} for k, v in out["share_by_vendor"].items()])
                if out.get("share_after_conditional_discounts_inr"):
                    add_table(title + " · after conditional discounts", [{"vendor": k, "extended_inr_after_discount": v} for k, v in out["share_after_conditional_discounts_inr"].items()])
            elif "options" in out:
                add_table(title, out["options"])
            elif "vendors" in out and isinstance(out["vendors"], list):
                add_table(title, out["vendors"])
            elif "items" in out:
                add_table(title, out["items"])
            elif name == "sensitivity":
                add_table(title, [{"metric": "total_before_inr", "value": out["total_before_inr"]}, {"metric": "total_after_inr", "value": out["total_after_inr"]}])
                add_table(title + " · lines that change winner", out["lines_that_change_winner"])
            elif name in ("questionnaire_overview", "commercial_terms"):
                add_table(title, [{"vendor": k, **{kk: (", ".join(str(x) for x in vv) if isinstance(vv, list) and not (vv and isinstance(vv[0], dict)) else (", ".join(f"{c['name']}{' (attached)' if c.get('attached') else ' (claimed only)' if c.get('claimed_only') else ''}" for c in vv) if isinstance(vv, list) else vv)) for kk, vv in v.items()}} for k, v in out.items()])
            elif name == "calculate":
                add_table("calculate", [{"label": out.get("label"), "expression": out.get("expression"), "result": out.get("result")}])
            elif name == "fx_rates":
                add_table(title, [{"currency": k, "to_inr": v} for k, v in out["rates_to_inr"].items()] + [{"currency": "as_of", "to_inr": out["as_of"]}])
    return tables, charts, caveats


def ask(state: dict, question: str, history: list[dict], log: list | None = None) -> dict:
    cmp = engine.build_comparison(state)
    messages = []
    for h in history[-6:]:
        messages.append({"role": "user", "content": h["question"]})
        messages.append({"role": "assistant", "content": h["answer"] or "(no answer)"})
    messages.append({"role": "user", "content": _context_summary(cmp) + "\n\nBuyer's question: " + question})
    text, trace = llm.agent_loop(purpose="analyst", system=SYSTEM, messages=messages, tools=TOOLS, execute=make_executor(cmp), max_rounds=8, log=log)
    tables, charts, caveats = tables_from_trace(trace)
    return {"question": question, "answer": text, "trace": trace, "tables": tables, "charts": charts, "caveats": caveats}

"""Agent 5 — Analyst: natural-language Q&A over a normalized vendor comparison.

Arithmetic (cheapest-per-line, totals, markdown tables) is always deterministic
Python. Claude explains and recommends using only data injected from the loaded
comparison — never invented prices or hardcoded demo answers.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any, Optional, Union

from shared_models import ComparisonTable, NormalizedCell, RFx

DEFAULT_MODEL = "claude-3-5-sonnet-20240620"
DEFAULT_USD_RATE = 83.50
USABLE_STATUSES = frozenset({"ok", "converted"})

ComparisonLike = Union[ComparisonTable, dict[str, Any]]
RFxLike = Union[RFx, dict[str, Any], None]


def _model_name() -> str:
    return os.environ.get("ANTHROPIC_SONNET_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _cell_as_dict(cell: Any) -> dict[str, Any]:
    if isinstance(cell, NormalizedCell):
        return cell.model_dump()
    if isinstance(cell, dict):
        return dict(cell)
    if hasattr(cell, "model_dump"):
        return cell.model_dump()
    return {
        "line_id": getattr(cell, "line_id", None),
        "vendor_id": getattr(cell, "vendor_id", None),
        "unit_price_inr": getattr(cell, "unit_price_inr", None),
        "status": getattr(cell, "status", "missing"),
        "original_price": getattr(cell, "original_price", None),
        "original_currency": getattr(cell, "original_currency", None),
        "original_uom": getattr(cell, "original_uom", None),
        "flags": list(getattr(cell, "flags", []) or []),
    }


def _usable(cell: dict[str, Any]) -> bool:
    status = str(cell.get("status") or "")
    return status in USABLE_STATUSES and cell.get("unit_price_inr") is not None


def compute_cheapest_per_qualified(
    cells: list[dict[str, Any]],
    qualified_vendors: Optional[list[str]] = None,
    vendor_names: Optional[dict[str, str]] = None,
    line_meta: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Pure: min unit_price_inr per line among qualified vendors with usable prices."""
    names = vendor_names or {}
    meta = line_meta or {}
    qual = set(qualified_vendors) if qualified_vendors is not None else None

    by_line: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in cells:
        cell = _cell_as_dict(raw)
        if not _usable(cell):
            continue
        vid = cell.get("vendor_id")
        if qual is not None and vid not in qual:
            continue
        by_line[str(cell.get("line_id"))].append(cell)

    rows: list[dict[str, Any]] = []
    for line_id in sorted(by_line.keys()):
        best = min(by_line[line_id], key=lambda c: float(c["unit_price_inr"]))
        vid = best["vendor_id"]
        qty = meta.get(line_id, {}).get("qty")
        row: dict[str, Any] = {
            "line_id": line_id,
            "vendor_id": vid,
            "vendor_name": names.get(vid, vid),
            "unit_price_inr": float(best["unit_price_inr"]),
            "status": best.get("status"),
        }
        if qty is not None:
            try:
                qf = float(qty)
                row["qty"] = qf
                row["extended_inr"] = round(qf * float(best["unit_price_inr"]), 2)
            except (TypeError, ValueError):
                pass
        rows.append(row)
    return rows


def compute_vendor_totals(
    cells: list[dict[str, Any]],
    line_meta: Optional[dict[str, dict[str, Any]]] = None,
    qualified_vendors: Optional[list[str]] = None,
    vendor_names: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Pure: approximate extended totals per vendor from usable cells."""
    meta = line_meta or {}
    names = vendor_names or {}
    qual = set(qualified_vendors) if qualified_vendors is not None else None
    totals: dict[str, float] = defaultdict(float)
    coverage: dict[str, int] = defaultdict(int)

    for raw in cells:
        cell = _cell_as_dict(raw)
        if not _usable(cell):
            continue
        vid = cell.get("vendor_id")
        if qual is not None and vid not in qual:
            continue
        lid = str(cell.get("line_id"))
        qty = meta.get(lid, {}).get("qty", 1.0)
        try:
            qf = float(qty) if qty is not None else 1.0
        except (TypeError, ValueError):
            qf = 1.0
        totals[vid] += float(cell["unit_price_inr"]) * qf
        coverage[vid] += 1

    return [
        {
            "vendor_id": vid,
            "vendor_name": names.get(vid, vid),
            "approx_total_inr": round(tot, 2),
            "lines_covered": coverage[vid],
        }
        for vid, tot in sorted(totals.items(), key=lambda kv: kv[1])
    ]


def build_comparison_markdown(
    cells: list[dict[str, Any]],
    vendor_names: Optional[dict[str, str]] = None,
) -> str:
    """Pure: markdown matrix of line × vendor INR unit prices (— if missing)."""
    names = vendor_names or {}
    parsed = [_cell_as_dict(c) for c in cells]
    vendors = sorted({c["vendor_id"] for c in parsed if c.get("vendor_id")})
    lines = sorted({str(c["line_id"]) for c in parsed if c.get("line_id") is not None})

    price_map: dict[tuple[str, str], Optional[float]] = {}
    for c in parsed:
        key = (str(c.get("line_id")), c.get("vendor_id"))
        if _usable(c):
            price_map[key] = float(c["unit_price_inr"])
        elif key not in price_map:
            price_map[key] = None

    headers = ["line_id"] + [names.get(v, v) for v in vendors]
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for lid in lines:
        row = [lid]
        for v in vendors:
            val = price_map.get((lid, v))
            row.append(f"{val:.2f}" if val is not None else "—")
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def _enrichment_from_rfx(rfx: RFxLike) -> dict[str, Any]:
    if rfx is None:
        return {}
    if isinstance(rfx, RFx):
        data = rfx.model_dump()
    elif isinstance(rfx, dict):
        data = rfx
    elif hasattr(rfx, "model_dump"):
        data = rfx.model_dump()
    else:
        return {}

    vendor_names: dict[str, str] = {}
    for v in data.get("vendors") or []:
        if isinstance(v, dict):
            vendor_names[str(v.get("vendor_id"))] = str(v.get("name") or v.get("vendor_id"))
        else:
            vendor_names[str(getattr(v, "vendor_id", ""))] = str(
                getattr(v, "name", getattr(v, "vendor_id", ""))
            )

    line_meta: dict[str, dict[str, Any]] = {}
    for li in data.get("line_items") or []:
        if isinstance(li, dict):
            lid = str(li.get("line_id"))
            line_meta[lid] = {
                "description": li.get("description", ""),
                "qty": li.get("qty"),
                "uom": li.get("uom", "piece"),
            }
        else:
            lid = str(getattr(li, "line_id", ""))
            line_meta[lid] = {
                "description": getattr(li, "description", ""),
                "qty": getattr(li, "qty", None),
                "uom": getattr(li, "uom", "piece"),
            }

    questionnaire = data.get("questionnaire") or []
    return {
        "rfx_id": data.get("rfx_id"),
        "vendor_names": vendor_names,
        "line_meta": line_meta,
        "questionnaire": questionnaire,
        "title": data.get("title"),
        "currency": data.get("currency", "INR"),
    }


def normalize_comparison_state(
    comparison: ComparisonLike,
    rfx: RFxLike = None,
) -> dict[str, Any]:
    """Normalize ComparisonTable/dict (+ optional RFx) into AnalystAgent state."""
    if comparison is None:
        raise ValueError("comparison is required")

    if isinstance(comparison, ComparisonTable):
        base = comparison.model_dump()
    elif isinstance(comparison, dict):
        base = dict(comparison)
    elif hasattr(comparison, "model_dump"):
        base = comparison.model_dump()
    else:
        raise TypeError(f"Unsupported comparison type: {type(comparison)!r}")

    cells = [_cell_as_dict(c) for c in (base.get("cells") or [])]
    enrich = _enrichment_from_rfx(rfx)

    vendor_names = dict(enrich.get("vendor_names") or {})
    vendor_names.update({str(k): str(v) for k, v in (base.get("vendor_names") or {}).items()})

    line_meta = dict(enrich.get("line_meta") or {})
    for k, v in (base.get("line_meta") or {}).items():
        line_meta[str(k)] = dict(v) if isinstance(v, dict) else {"description": str(v)}

    qualified = base.get("qualified_vendors")
    if qualified is None:
        # Default: all vendors that appear in cells (callers should pass explicit list)
        qualified = sorted({c["vendor_id"] for c in cells if c.get("vendor_id")})

    usd_rate = base.get("usd_rate", DEFAULT_USD_RATE)
    try:
        usd_rate = float(usd_rate)
    except (TypeError, ValueError):
        usd_rate = DEFAULT_USD_RATE

    return {
        "rfx_id": base.get("rfx_id") or enrich.get("rfx_id") or "",
        "cells": cells,
        "vendor_flags": dict(base.get("vendor_flags") or {}),
        "vendor_names": vendor_names,
        "line_meta": line_meta,
        "qualified_vendors": list(qualified),
        "usd_rate": usd_rate,
        "questionnaire_by_vendor": dict(base.get("questionnaire_by_vendor") or {}),
        "questionnaire": enrich.get("questionnaire") or base.get("questionnaire") or [],
        "title": enrich.get("title") or base.get("title"),
        "currency": enrich.get("currency") or base.get("currency") or "INR",
    }


def _has_usd_converted(cells: list[dict[str, Any]]) -> bool:
    for c in cells:
        cell = _cell_as_dict(c)
        cur = (cell.get("original_currency") or "").upper()
        if cell.get("status") == "converted" and cur in {"USD", "US$", "$"}:
            return True
        flags = " ".join(cell.get("flags") or []).lower()
        if "usd" in flags or "fx" in flags:
            return True
    return False


def _safe_calculate(expression: str) -> float:
    """Deterministic calculator: numbers, + - * / **, parentheses, percent(a,b)."""
    import ast
    import operator as op

    allowed_bin = {
        ast.Add: op.add,
        ast.Sub: op.sub,
        ast.Mult: op.mul,
        ast.Div: op.truediv,
        ast.Pow: op.pow,
    }
    allowed_unary = {ast.UAdd: op.pos, ast.USub: op.neg}

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_bin:
            return allowed_bin[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_unary:
            return allowed_unary[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "percent":
            if len(node.args) != 2:
                raise ValueError("percent(a, b) needs two args")
            a, b = _eval(node.args[0]), _eval(node.args[1])
            if b == 0:
                raise ZeroDivisionError("percent denominator is 0")
            return (a / b) * 100.0
        raise ValueError(f"disallowed expression node: {type(node).__name__}")

    tree = ast.parse(expression.strip(), mode="eval")
    return float(_eval(tree))


SYSTEM_RULES = """You are a sourcing analyst helping a category buyer compare vendor quotes.
Rules:
- NEVER invent prices, totals, winners, or quantities. Every number you state must come from COMPARISON_DATA or PRECOMPUTED blocks in this prompt / conversation.
- If a figure is missing or uncertain, say so explicitly and cite the cell status/flags.
- Prefer qualified vendors (those who cleared knockout questionnaire items) when the buyer asks for cleared-only / qualified analysis.
- When USD-converted cells exist, flag FX risk and mention the fixed USD→INR rate from the data.
- For award questions: recommend using precomputed cheapest-qualified line winners; summarize total spend from those rows; list risk flags and coverage gaps.
- Refer to vendors by name when names are provided. Amounts are INR unless noted.
- Keep answers tight: short paragraphs and bullets. Do not dump the full matrix unless asked.
"""


class AnalystAgent:
    """Stateful analyst with multi-turn chat over one loaded comparison."""

    def __init__(self, client=None):
        self._client = client
        self._client_provided = client is not None
        self.state: Optional[dict[str, Any]] = None
        self.chat_history: list[dict[str, str]] = []

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Pass client= to AnalystAgent or set the env var before ask()."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("anthropic package is required for AnalystAgent.ask()") from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def load_comparison(self, comparison: ComparisonLike, rfx: RFxLike = None) -> None:
        self.state = normalize_comparison_state(comparison, rfx=rfx)
        self.chat_history = []

    def reset(self) -> None:
        self.chat_history = []

    def _require_state(self) -> dict[str, Any]:
        if not self.state:
            raise RuntimeError("No comparison loaded. Call load_comparison() first.")
        return self.state

    def cheapest_per_qualified(self) -> list[dict[str, Any]]:
        st = self._require_state()
        return compute_cheapest_per_qualified(
            st["cells"],
            qualified_vendors=st.get("qualified_vendors"),
            vendor_names=st.get("vendor_names"),
            line_meta=st.get("line_meta"),
        )

    def comparison_markdown_table(self) -> str:
        st = self._require_state()
        return build_comparison_markdown(st["cells"], st.get("vendor_names"))

    def vendor_totals(self, qualified_only: bool = False) -> list[dict[str, Any]]:
        st = self._require_state()
        qual = st.get("qualified_vendors") if qualified_only else None
        return compute_vendor_totals(
            st["cells"],
            line_meta=st.get("line_meta"),
            qualified_vendors=qual,
            vendor_names=st.get("vendor_names"),
        )

    def _serialize_context(self) -> dict[str, Any]:
        st = self._require_state()
        cells_compact = []
        for c in st["cells"]:
            cells_compact.append(
                {
                    "line_id": c.get("line_id"),
                    "vendor_id": c.get("vendor_id"),
                    "unit_price_inr": c.get("unit_price_inr"),
                    "status": c.get("status"),
                    "original_currency": c.get("original_currency"),
                    "original_price": c.get("original_price"),
                    "flags": c.get("flags") or [],
                }
            )
        return {
            "rfx_id": st.get("rfx_id"),
            "title": st.get("title"),
            "usd_rate": st.get("usd_rate"),
            "qualified_vendors": st.get("qualified_vendors"),
            "vendor_names": st.get("vendor_names"),
            "vendor_flags": st.get("vendor_flags"),
            "line_meta": st.get("line_meta"),
            "questionnaire_by_vendor": st.get("questionnaire_by_vendor"),
            "has_usd_converted_cells": _has_usd_converted(st["cells"]),
            "cells": cells_compact,
        }

    def _system_prompt(self) -> str:
        ctx = self._serialize_context()
        # Compact JSON — only data derived from loaded comparison
        payload = json.dumps(ctx, ensure_ascii=False, separators=(",", ":"), default=str)
        fx_note = ""
        if ctx.get("has_usd_converted_cells"):
            fx_note = (
                f"\nFX NOTE: USD-converted cells are present. Fixed rate USD→INR = {ctx.get('usd_rate')}. "
                "Flag FX risk in answers that depend on those cells.\n"
            )
        return f"{SYSTEM_RULES}{fx_note}\nCOMPARISON_DATA:\n{payload}"

    def ask(self, question: str) -> str:
        self._require_state()
        client = self._ensure_client()
        q = (question or "").strip()
        if not q:
            return "Ask a question about the loaded comparison."

        messages = list(self.chat_history) + [{"role": "user", "content": q}]
        resp = client.messages.create(
            model=_model_name(),
            max_tokens=4096,
            system=self._system_prompt(),
            messages=messages,
        )
        text = _extract_text(resp)
        self.chat_history.append({"role": "user", "content": q})
        self.chat_history.append({"role": "assistant", "content": text})
        return text

    def get_award_recommendation(self) -> str:
        cheapest = self.cheapest_per_qualified()
        totals = self.vendor_totals(qualified_only=True)
        st = self._require_state()
        total_spend = round(
            sum(r.get("extended_inr") or 0 for r in cheapest if r.get("extended_inr") is not None),
            2,
        )
        lines_without_ext = [r["line_id"] for r in cheapest if "extended_inr" not in r]
        prompt = (
            "Give an award recommendation I can defend to a VP.\n"
            "Structure your reply with these sections:\n"
            "1. Executive summary\n"
            "2. Qualification (who cleared knockouts / who is excluded)\n"
            "3. Line-by-line award using the PRECOMPUTED cheapest qualified winners (do not change winners)\n"
            "4. Total spend (use PRECOMPUTED_TOTAL_SPEND_INR when extended values exist)\n"
            "5. Risk flags (vendor_flags, FX, freight, gaps)\n"
            "6. Gaps / clarifications still needed\n\n"
            f"PRECOMPUTED_CHEAPEST_QUALIFIED:\n{json.dumps(cheapest, ensure_ascii=False, default=str)}\n\n"
            f"PRECOMPUTED_VENDOR_TOTALS_QUALIFIED:\n{json.dumps(totals, ensure_ascii=False, default=str)}\n\n"
            f"PRECOMPUTED_TOTAL_SPEND_INR: {total_spend}\n"
            f"QUALIFIED_VENDOR_IDS: {st.get('qualified_vendors')}\n"
            f"VENDOR_FLAGS: {json.dumps(st.get('vendor_flags') or {}, ensure_ascii=False)}\n"
            f"LINES_MISSING_QTY_FOR_EXTENDED: {lines_without_ext}\n"
        )
        return self.ask(prompt)


def _extract_text(resp: Any) -> str:
    parts: list[str] = []
    content = getattr(resp, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p).strip() or str(resp)


def _question_intents(question: str) -> set[str]:
    q = (question or "").lower()
    intents: set[str] = set()
    if re.search(r"cheapest|per[- ]line|split", q):
        intents.add("cheapest")
    if re.search(r"qualif|knockout|cleared|questionnaire", q):
        intents.add("qualified")
    if re.search(r"award|recommend|vp|exec", q):
        intents.add("award")
    if re.search(r"single[- ]vendor|sole supplier|one supplier", q):
        intents.add("single")
    if re.search(r"total|rank|overall", q):
        intents.add("totals")
    if re.search(r"table|matrix|markdown|side[- ]by[- ]side", q):
        intents.add("table")
    if re.search(r"usd|fx|rupee|exchange", q):
        intents.add("fx")
    if re.search(r"missing|gap|coverage|clarif", q):
        intents.add("gaps")
    return intents


def _gaps(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for c in cells:
        cell = _cell_as_dict(c)
        if cell.get("status") in {"missing", "uncertain", "uom_mismatch"} or cell.get("unit_price_inr") is None:
            if cell.get("status") in USABLE_STATUSES and cell.get("unit_price_inr") is not None:
                continue
            if str(cell.get("status")) in USABLE_STATUSES:
                continue
            out.append(
                {
                    "line_id": cell.get("line_id"),
                    "vendor_id": cell.get("vendor_id"),
                    "status": cell.get("status"),
                    "flags": cell.get("flags") or [],
                }
            )
    return out


def answer(question: str, rfx: RFxLike = None, comparison: ComparisonLike = None) -> dict[str, Any]:
    """Manager-facing entrypoint. Returns answer prose plus deterministic tables.

    Keys: answer, markdown, tables, data, caveats, tool, model.
    Uses Claude when available; deterministic tables are always computed in Python.
    """
    if comparison is None:
        raise ValueError("comparison is required")

    agent = AnalystAgent()
    agent.load_comparison(comparison, rfx=rfx)
    st = agent.state
    assert st is not None

    intents = _question_intents(question)
    qual_only = "qualified" in intents or "award" in intents

    cheapest_all = compute_cheapest_per_qualified(
        st["cells"],
        qualified_vendors=None,
        vendor_names=st.get("vendor_names"),
        line_meta=st.get("line_meta"),
    )
    cheapest_qual = agent.cheapest_per_qualified()
    md = agent.comparison_markdown_table()
    totals_all = agent.vendor_totals(qualified_only=False)
    totals_qual = agent.vendor_totals(qualified_only=True)
    gaps = _gaps(st["cells"])
    caveats = [
        f"Vendor flags: {vid} → {flags}"
        for vid, flags in (st.get("vendor_flags") or {}).items()
        if flags
    ]
    if _has_usd_converted(st["cells"]):
        caveats.append(
            f"FX risk: USD-converted cells present at fixed rate {st.get('usd_rate')} INR/USD."
        )

    tables: dict[str, Any] = {
        "cheapest_per_qualified": cheapest_qual,
        "cheapest_per_line_all": cheapest_all,
        "vendor_totals": totals_qual if qual_only else totals_all,
        "vendor_totals_all": totals_all,
        "vendor_totals_qualified": totals_qual,
        "gaps": gaps[:100],
        "comparison_markdown": md,
    }

    # Prefetch deterministic winners into the user message so Claude cannot invent them
    inject_bits = [
        f"PRECOMPUTED_CHEAPEST_QUALIFIED:\n{json.dumps(cheapest_qual, ensure_ascii=False, default=str)}",
        f"PRECOMPUTED_VENDOR_TOTALS_QUALIFIED:\n{json.dumps(totals_qual, ensure_ascii=False, default=str)}",
        f"QUALIFIED_VENDOR_IDS: {st.get('qualified_vendors')}",
    ]
    if "single" in intents:
        inject_bits.append(
            "BUYER CONSTRAINT: prefer discussing single-vendor award vs split; "
            "use vendor_totals_qualified for sole-supplier ranking (coverage gaps matter)."
        )
    if "award" in intents:
        spend = round(sum(r.get("extended_inr") or 0 for r in cheapest_qual if "extended_inr" in r), 2)
        inject_bits.append(f"PRECOMPUTED_SPLIT_TOTAL_SPEND_INR: {spend}")

    user_msg = (question or "").strip()
    if inject_bits and any(i in intents for i in ("cheapest", "award", "totals", "single", "qualified", "fx", "gaps")):
        user_msg = user_msg + "\n\n" + "\n\n".join(inject_bits)

    tool = "llm_ask"
    try:
        if "award" in intents and not any(
            x in (question or "").lower() for x in ("what if", "sensitivity", "apply discount")
        ):
            text = agent.get_award_recommendation()
            tool = "award_recommendation"
        else:
            text = agent.ask(user_msg)
    except RuntimeError as exc:
        # No API key / client — still return deterministic tables so callers/tests work offline
        text = _offline_answer(question, intents, tables, caveats, st)
        tool = "deterministic_offline"
        caveats.append(f"LLM unavailable: {exc}")

    return {
        "answer": text,
        "markdown": md,
        "tables": tables,
        "data": cheapest_qual if qual_only or "cheapest" in intents else tables.get("vendor_totals"),
        "caveats": caveats,
        "tool": tool,
        "model": _model_name(),
    }


def _offline_answer(
    question: str,
    intents: set[str],
    tables: dict[str, Any],
    caveats: list[str],
    st: dict[str, Any],
) -> str:
    """Deterministic prose when Claude cannot be called (tests / missing key)."""
    lines: list[str] = []
    if "award" in intents or "cheapest" in intents:
        rows = tables["cheapest_per_qualified"]
        spend = round(sum(r.get("extended_inr") or 0 for r in rows if "extended_inr" in r), 2)
        lines.append("## Award / split (qualified vendors only)")
        lines.append(f"Qualified vendors: {', '.join(st.get('qualified_vendors') or []) or '(none)'}")
        lines.append(f"Lines awarded: {len(rows)}; precomputed split spend (where qty known): ₹{spend:,.2f}")
        for r in rows[:20]:
            ext = f", extended ₹{r['extended_inr']:,.2f}" if "extended_inr" in r else ""
            lines.append(
                f"- {r['line_id']}: {r.get('vendor_name') or r['vendor_id']} @ ₹{r['unit_price_inr']:.2f}{ext}"
            )
        if len(rows) > 20:
            lines.append(f"- … {len(rows) - 20} more lines")
    elif "totals" in intents or "single" in intents:
        lines.append("## Vendor totals (usable cells)")
        for t in tables["vendor_totals"]:
            lines.append(
                f"- {t.get('vendor_name') or t['vendor_id']}: ₹{t['approx_total_inr']:,.2f} "
                f"({t['lines_covered']} lines)"
            )
    elif "gaps" in intents:
        g = tables["gaps"]
        lines.append(f"## Coverage gaps ({len(g)} cells shown)")
        for row in g[:30]:
            lines.append(f"- {row['line_id']} / {row['vendor_id']}: {row['status']}")
    elif "table" in intents:
        lines.append(tables["comparison_markdown"])
    else:
        lines.append(
            "Deterministic summary (LLM offline). "
            f"{len(st.get('cells') or [])} cells; "
            f"{len(st.get('qualified_vendors') or [])} qualified vendors. "
            "Ask about cheapest-per-line, totals, gaps, or award recommendation."
        )
        lines.append(f"Question: {question!r}")
    if caveats:
        lines.append("\nCaveats:")
        lines.extend(f"- {c}" for c in caveats)
    return "\n".join(lines)


def ask_question(
    question: Any = None,
    rfx: RFxLike = None,
    comparison: ComparisonLike = None,
    table: ComparisonLike = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compat entrypoint.

    Preferred (manager): ask_question(question, rfx, comparison)
    Legacy stub: ask_question(table, question, rfx=...)
    """
    # Keyword path
    if isinstance(question, str) and (comparison is not None or table is not None or kwargs.get("comparison") is not None):
        return answer(question, rfx=rfx, comparison=comparison if comparison is not None else table)

    # Legacy positional: ask_question(table, question, rfx)
    if question is not None and not isinstance(question, str):
        legacy_table = question
        legacy_q = rfx if isinstance(rfx, str) else (comparison if isinstance(comparison, str) else "")
        legacy_rfx = None
        if not isinstance(rfx, str):
            legacy_rfx = rfx
        # signature ask_question(table, question, rfx=None)
        if isinstance(rfx, str):
            legacy_q = rfx
            legacy_rfx = comparison if not isinstance(comparison, str) else None
        return answer(str(legacy_q or ""), rfx=legacy_rfx, comparison=legacy_table)

    if isinstance(question, str) and comparison is None and table is None:
        # Maybe only question + rfx kwargs missing comparison — raise clearly
        raise ValueError("comparison (or table=) is required")

    return answer(str(question or ""), rfx=rfx, comparison=comparison if comparison is not None else table)


# Stub-compatible module helpers (operate on ComparisonTable)
def cheapest_per_line(table: ComparisonTable) -> list[dict[str, Any]]:
    st = normalize_comparison_state(table)
    return compute_cheapest_per_qualified(
        st["cells"],
        qualified_vendors=None,
        vendor_names=st.get("vendor_names"),
        line_meta=st.get("line_meta"),
    )


def vendor_totals(table: ComparisonTable, rfx: Optional[RFx] = None) -> list[dict[str, Any]]:
    st = normalize_comparison_state(table, rfx=rfx)
    return compute_vendor_totals(
        st["cells"],
        line_meta=st.get("line_meta"),
        qualified_vendors=None,
        vendor_names=st.get("vendor_names"),
    )

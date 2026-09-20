"""Orchestrator — free tabbed workspace: Draft | Send | Inbox | Compare | Ask | Award.

Wires public agent APIs; persists snapshots under data/store/.
Navigation is free — wizard_step is the last-open tab, not a lock gate.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from agents.analyst import ask_question, suggest_split_award, validate_award
from agents.document_parser import (
    list_inbox,
    parse_all,
    parse_one,
    seed_inbox,
)
from agents.normalizer import normalize
from agents.rfx_drafter import apply_rfx_edits, draft_rfx, regenerate_line_items
from agents.vendor_dispatcher import (
    VendorDispatcherAgent,
    preview_cover_emails,
)
from shared_models import (
    ComparisonTable,
    ExtractedQuote,
    InboxMessage,
    LineItem,
    RFx,
)

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "data" / "vendor_responses"
STORE_DIR = ROOT / "data" / "store"
OUTBOX_DIR = ROOT / "data" / "outbox"
INBOX_DIR = ROOT / "data" / "inbox"

WIZARD_STEPS = ["draft", "send", "inbox", "compare", "ask", "award"]

_STRUCTURED = {".json", ".csv"}


def _has_anthropic() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _to_quote(payload: Any, rfx: Optional[RFx] = None) -> ExtractedQuote:
    if isinstance(payload, ExtractedQuote):
        return payload
    if isinstance(payload, (str, Path)):
        return parse_one(payload, rfx=rfx)
    if isinstance(payload, dict):
        fields = {k: payload[k] for k in ExtractedQuote.model_fields if k in payload}
        if "source_format" not in fields:
            fields["source_format"] = (
                payload.get("source_format")
                or payload.get("parse_method")
                or "unknown"
            )
        if "vendor_id" not in fields:
            fields["vendor_id"] = payload.get("vendor_id") or "UNKNOWN"
        if "notes" not in fields:
            fields["notes"] = (
                payload.get("notes") or payload.get("extraction_notes") or ""
            )
        return ExtractedQuote.model_validate(fields)
    raise TypeError(f"Cannot coerce quote from {type(payload)!r}")


class RFxPipeline:
    """In-memory crew session for one sourcing event (tab-aware)."""

    def __init__(self) -> None:
        self.brief: str = ""
        self.rfx: Optional[RFx] = None
        self.dispatch_log: list[dict[str, Any]] = []
        self.dispatch_paths: list[str] = []
        self.cover_previews: list[dict[str, str]] = []
        self.quotes: list[ExtractedQuote] = []
        self.comparison: Optional[ComparisonTable] = None
        self.chat: list[dict[str, Any]] = []
        self.analyst_history: list[dict[str, str]] = []
        self.inbox: list[InboxMessage] = []
        self.awards: dict[str, str] = {}
        self.award_validation: dict[str, Any] = {}
        self.step: str = "idle"
        self.wizard_step: str = "draft"

    # ── Draft ──────────────────────────────────────────────────────────

    def start_draft(
        self,
        brief: str,
        *,
        title: str = "",
        scope: str = "",
        terms: str = "",
    ) -> None:
        self.brief = brief.strip()
        rid = f"RFX-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        self.rfx = RFx(
            rfx_id=rid,
            title=title.strip() or "Untitled RFx",
            scope=scope.strip() or brief.strip()[:500],
            terms=terms.strip()
            or "INR per piece, delivered, exclusive of GST. Payment 45 days.",
            line_items=[],
            questionnaire=[],
            vendors=[],
        )
        self.step = "drafting"
        self.wizard_step = "draft"
        self._persist()

    def update_draft_fields(
        self,
        *,
        brief: Optional[str] = None,
        title: Optional[str] = None,
        scope: Optional[str] = None,
        terms: Optional[str] = None,
        currency: Optional[str] = None,
    ) -> RFx:
        if not self.rfx:
            raise RuntimeError("No draft session.")
        if brief is not None:
            self.brief = brief.strip()
        edits: dict[str, Any] = {}
        if title is not None:
            edits["title"] = title.strip() or self.rfx.title
        if scope is not None:
            edits["scope"] = scope.strip()
        if terms is not None:
            edits["terms"] = terms.strip()
        if currency is not None:
            edits["currency"] = currency.strip() or self.rfx.currency
        if edits:
            self.rfx = apply_rfx_edits(self.rfx, **edits)
        self._persist()
        return self.rfx

    def draft(self, brief: str, **kwargs: Any) -> RFx:
        """Generate line items + questionnaire + vendors via RFx Drafter."""
        self.brief = (brief or self.brief or "").strip()
        if len(self.brief) < 10:
            raise ValueError("Brief is too short — describe category, volume, and site.")

        overlays: dict[str, Any] = {}
        if self.rfx:
            kwargs.setdefault("rfx_id", self.rfx.rfx_id)
            if self.rfx.title and self.rfx.title != "Untitled RFx":
                overlays["title"] = self.rfx.title
            if self.rfx.scope and self.rfx.scope.strip():
                overlays["scope"] = self.rfx.scope
            if self.rfx.terms and self.rfx.terms.strip():
                default_terms = "INR per piece, delivered, exclusive of GST. Payment 45 days."
                if self.rfx.terms.strip() != default_terms:
                    overlays["terms"] = self.rfx.terms
            overlays.update({k: kwargs[k] for k in ("title", "scope", "terms", "currency", "vendors") if k in kwargs})
        else:
            overlays = {k: kwargs[k] for k in ("title", "scope", "terms", "currency", "vendors") if k in kwargs}

        call_kwargs = {k: v for k, v in kwargs.items() if k not in overlays}
        if "rfx_id" in kwargs:
            call_kwargs["rfx_id"] = kwargs["rfx_id"]
        self.rfx = draft_rfx(self.brief, **call_kwargs, **overlays)
        self.step = "drafted"
        self.wizard_step = "draft"
        self.refresh_cover_previews()
        self._persist()
        return self.rfx

    def regenerate_lines(self, brief: Optional[str] = None, **kwargs: Any) -> RFx:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        self.brief = (brief or self.brief or "").strip()
        kwargs.setdefault("refresh_questionnaire", True)
        kwargs.setdefault("refresh_vendors", True)
        self.rfx = regenerate_line_items(self.rfx, brief=self.brief or None, **kwargs)
        self.step = "drafted"
        self.refresh_cover_previews()
        self._persist()
        return self.rfx

    def update_line_items(self, items: list[dict[str, Any]]) -> RFx:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        lines: list[LineItem] = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                continue
            desc = str(raw.get("description") or "").strip()
            if not desc:
                continue
            lid = str(raw.get("line_id") or f"L{i+1:02d}").strip()
            try:
                qty = float(raw.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            uom = str(raw.get("uom") or "piece")
            specs = raw.get("specs") if isinstance(raw.get("specs"), dict) else {}
            lines.append(
                LineItem(line_id=lid, description=desc, qty=qty, uom=uom, specs=specs)
            )
        if not lines:
            raise ValueError("At least one line item is required.")
        self.rfx = apply_rfx_edits(self.rfx, line_items=lines)
        self.refresh_cover_previews()
        self._persist()
        return self.rfx

    # ── Send ───────────────────────────────────────────────────────────

    def refresh_cover_previews(self) -> list[dict[str, str]]:
        if not self.rfx or not self.rfx.vendors:
            self.cover_previews = []
            return []
        self.cover_previews = preview_cover_emails(self.rfx)
        return self.cover_previews

    def preview_cover_email(self) -> str:
        """Legacy single-string preview (first vendor)."""
        previews = self.refresh_cover_previews()
        if not previews:
            return ""
        p = previews[0]
        return f"To: {p.get('to','')}\nSubject: {p.get('subject','')}\n\n{p.get('body','')}"

    def dispatch(self, *, stub: bool = True) -> list[dict[str, Any]]:
        if not self.rfx:
            raise RuntimeError("Draft an RFx before dispatching.")
        if not self.rfx.line_items:
            raise RuntimeError("Generate line items before sending.")
        if not self.rfx.vendors:
            raise RuntimeError("RFx has no vendors to send to.")
        self.refresh_cover_previews()
        agent = VendorDispatcherAgent(stub=stub, outbox_dir=OUTBOX_DIR)
        self.dispatch_paths = [str(p) for p in agent.dispatch(self.rfx)]
        self.dispatch_log = list(agent.dispatch_log)
        for row in self.dispatch_log:
            row.setdefault(
                "delivery", row.get("status") or row.get("delivery") or "stubbed"
            )
        self.step = "dispatched"
        self.wizard_step = "send"
        self._persist()
        return self.dispatch_log

    # ── Inbox ──────────────────────────────────────────────────────────

    def _inbox_dir(self) -> Path:
        assert self.rfx
        d = INBOX_DIR / self.rfx.rfx_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def seed_inbox(self, *, force: bool = False) -> list[InboxMessage]:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        inbox_root = self._inbox_dir()
        if self.inbox and not force:
            return self.inbox

        seed_inbox(inbox_dir=inbox_root, vendor_dir=VENDOR_DIR)
        rows = list_inbox(inbox_dir=inbox_root)
        vendor_by_id = {v.vendor_id: v for v in self.rfx.vendors}
        # Soft map V01 ↔ V1
        soft = {
            re.sub(r"^V0+", "V", v.vendor_id.upper()): v for v in self.rfx.vendors
        }

        messages: list[InboxMessage] = []
        for row in rows:
            vid = str(row.get("vendor_id") or "")
            vendor = vendor_by_id.get(vid)
            if not vendor:
                soft_key = re.sub(r"^V0+", "V", vid.upper())
                vendor = soft.get(soft_key)
                if vendor:
                    vid = vendor.vendor_id
            path = Path(row["path"])
            try:
                preview = path.read_text(encoding="utf-8", errors="replace")[:280]
            except Exception:
                preview = f"(file: {path.name})"
            messages.append(
                InboxMessage(
                    msg_id=f"msg-{uuid.uuid4().hex[:8]}",
                    vendor_id=vid,
                    vendor_name=vendor.name if vendor else path.stem,
                    subject=f"Re: RFx {self.rfx.rfx_id} — quotation",
                    from_addr=vendor.email if vendor else f"quotes@{path.stem.lower()}.example",
                    path=str(path),
                    body_preview=preview.replace("\n", " ")[:240],
                    status="new",
                )
            )
        self.inbox = messages
        self.step = "inbox_ready"
        self._persist()
        return self.inbox

    def add_inbox_upload(
        self, path: Union[str, Path], *, vendor_id: str = ""
    ) -> InboxMessage:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        p = Path(path)
        dest = self._inbox_dir() / p.name
        if p.resolve() != dest.resolve():
            shutil.copy2(p, dest)
        vendor = next((v for v in self.rfx.vendors if v.vendor_id == vendor_id), None)
        try:
            preview = dest.read_text(encoding="utf-8", errors="replace")[:240]
        except Exception:
            preview = f"(uploaded: {dest.name})"
        msg = InboxMessage(
            msg_id=f"msg-{uuid.uuid4().hex[:8]}",
            vendor_id=vendor_id,
            vendor_name=vendor.name if vendor else dest.stem,
            subject=f"Upload — {dest.name}",
            from_addr=vendor.email if vendor else "upload@local",
            path=str(dest),
            body_preview=preview.replace("\n", " "),
            status="new",
        )
        self.inbox.append(msg)
        self._persist()
        return msg

    def parse_inbox_message(self, msg_id: str) -> ExtractedQuote:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        msg = next((m for m in self.inbox if m.msg_id == msg_id), None)
        if not msg:
            raise KeyError(f"Unknown inbox message {msg_id}")
        try:
            quote = parse_one(msg.path, vendor_id=msg.vendor_id or "", rfx=self.rfx)
            if msg.vendor_id and (
                not quote.vendor_id or quote.vendor_id.upper() in ("UNKNOWN", "")
            ):
                quote.vendor_id = msg.vendor_id
            msg.status = "parsed"
            msg.parsed_vendor_id = quote.vendor_id
            msg.error = ""
            self.quotes = [
                q for q in self.quotes if q.vendor_id != quote.vendor_id
            ] + [quote]
            self.step = "parsed"
            self.wizard_step = "inbox"
            self._persist()
            return quote
        except Exception as exc:
            msg.status = "error"
            msg.error = str(exc)
            self._persist()
            raise

    def parse_all_inbox(self, *, skip_llm: bool = False) -> list[ExtractedQuote]:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        if not self.inbox:
            self.seed_inbox()
        quotes: list[ExtractedQuote] = []
        errors: list[str] = []
        # Prefer per-message parse so one bad LLM/json blob cannot abort the batch
        for msg in list(self.inbox):
            try:
                q = parse_one(msg.path, vendor_id=msg.vendor_id or "", rfx=self.rfx)
                if msg.vendor_id and (
                    not q.vendor_id or q.vendor_id.upper() in ("UNKNOWN", "")
                ):
                    q.vendor_id = msg.vendor_id
                msg.status = "parsed"
                msg.parsed_vendor_id = q.vendor_id
                msg.error = ""
                quotes.append(q)
            except Exception as exc:  # noqa: BLE001
                msg.status = "error"
                msg.error = str(exc)
                errors.append(f"{msg.filename or msg.msg_id}: {exc}")
        if not quotes and errors:
            raise RuntimeError("; ".join(errors[:3]))
        self.quotes = quotes
        self.step = "parsed"
        self.wizard_step = "inbox"
        self._persist()
        return self.quotes

    def ingest(
        self,
        paths: Optional[list[Union[str, Path]]] = None,
        directory: Optional[Union[str, Path]] = None,
    ) -> list[ExtractedQuote]:
        if not self.rfx:
            raise RuntimeError("Draft an RFx before ingesting vendor files.")
        quotes: list[ExtractedQuote] = []
        if paths:
            for p in paths:
                quotes.append(parse_one(p, rfx=self.rfx))
        else:
            target = Path(directory) if directory else VENDOR_DIR
            quotes = parse_all(
                target,
                rfx=self.rfx,
                skip_llm=not _has_anthropic(),
            )
        self.quotes = quotes
        self.step = "parsed"
        self.wizard_step = "inbox"
        self._persist()
        return self.quotes

    # ── Compare ────────────────────────────────────────────────────────

    def normalize(self) -> ComparisonTable:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        if not self.quotes:
            raise RuntimeError("No vendor quotes parsed yet.")
        # Normalizer fills award_eligible_vendors / qualifications / questionnaire_results
        self.comparison = normalize(self.rfx, self.quotes)
        self.step = "normalized"
        self.wizard_step = "compare"
        self._persist()
        return self.comparison

    # ── Ask ────────────────────────────────────────────────────────────

    def ask(self, question: str) -> dict[str, Any]:
        if not self.comparison:
            raise RuntimeError("Normalize quotes before asking questions.")
        result = ask_question(
            question,
            self.rfx,
            self.comparison,
            history=self.analyst_history,
        )
        # Persist multi-turn history for the live chat panel
        hist = result.get("history")
        if isinstance(hist, list) and hist:
            self.analyst_history = [
                {"role": str(h.get("role", "")), "content": str(h.get("content", ""))}
                for h in hist
                if isinstance(h, dict)
            ]
        else:
            self.analyst_history.append({"role": "user", "content": question})
            self.analyst_history.append(
                {
                    "role": "assistant",
                    "content": result.get("answer") or result.get("markdown") or "",
                }
            )
        turn = {
            "question": question,
            "answer": result.get("answer") or result.get("markdown") or "",
            "data": result.get("data")
            if result.get("data") is not None
            else result.get("tables"),
            "caveats": result.get("caveats") or [],
            "tool": result.get("tool"),
            "model": result.get("model"),
            "raw": result,
        }
        self.chat.append(turn)
        self.step = "asked"
        self.wizard_step = "ask"
        self._persist()
        return turn

    # ── Award ──────────────────────────────────────────────────────────

    def qualified_vendor_ids(self) -> list[str]:
        if not self.comparison:
            return []
        return list(
            self.comparison.award_eligible_vendors
            or self.comparison.qualified_vendors
            or []
        )

    def eligible_vendors_for_line(self, line_id: str) -> list[dict[str, Any]]:
        if not self.comparison:
            return []
        qual = set(self.qualified_vendor_ids())
        names = dict(self.comparison.vendor_names or {})
        if self.rfx:
            names = {**{v.vendor_id: v.name for v in self.rfx.vendors}, **names}
        out: list[dict[str, Any]] = []
        for cell in self.comparison.cells:
            if cell.line_id != line_id:
                continue
            if cell.vendor_id not in qual:
                continue
            if cell.unit_price_inr is None:
                continue
            if cell.status in ("missing",):
                continue
            out.append(
                {
                    "vendor_id": cell.vendor_id,
                    "name": names.get(cell.vendor_id, cell.vendor_id),
                    "unit_price_inr": cell.unit_price_inr,
                    "status": cell.status,
                }
            )
        out.sort(key=lambda r: (r["unit_price_inr"] is None, r["unit_price_inr"] or 0))
        return out

    def save_awards(self, awards: dict[str, str]) -> dict[str, Any]:
        if not self.comparison or not self.rfx:
            raise RuntimeError("Compare quotes before awarding.")
        result = validate_award(
            self.comparison,
            qualifications=self.qualified_vendor_ids(),
            awards=awards,
            rfx=self.rfx,
        )
        cleaned: dict[str, str] = {}
        for row in result.get("awards") or []:
            lid = str(row.get("line_id") or "")
            vid = str(row.get("vendor_id") or "")
            if lid and vid:
                cleaned[lid] = vid
        self.awards = cleaned
        self.award_validation = result
        self.step = "awarded"
        self.wizard_step = "award"
        self._persist()
        return result

    def suggest_awards(self) -> dict[str, Any]:
        if not self.comparison:
            raise RuntimeError("Normalize first.")
        result = suggest_split_award(
            self.comparison,
            qualifications=self.qualified_vendor_ids(),
            rfx=self.rfx,
        )
        cleaned: dict[str, str] = {}
        for row in result.get("awards") or []:
            lid = str(row.get("line_id") or "")
            vid = str(row.get("vendor_id") or "")
            if lid and vid:
                cleaned[lid] = vid
        self.awards = cleaned
        self.award_validation = result
        self.step = "awarded"
        self.wizard_step = "award"
        self._persist()
        return result

    def award_summary(self) -> dict[str, Any]:
        if self.award_validation:
            totals = self.award_validation.get("totals") or {}
            return {
                "lines": self.award_validation.get("awards") or [],
                "total_inr": totals.get("grand_total_inr")
                or totals.get("grand_total_partial_inr")
                or 0.0,
                "by_vendor": totals.get("by_vendor") or {},
                "rejected": self.award_validation.get("rejected") or [],
                "markdown": self.award_validation.get("markdown") or "",
            }
        # Fallback compute
        if not self.rfx or not self.comparison:
            return {"lines": [], "total_inr": 0.0, "by_vendor": {}}
        price_map = {
            (c.line_id, c.vendor_id): c.unit_price_inr for c in self.comparison.cells
        }
        names = {v.vendor_id: v.name for v in self.rfx.vendors}
        names.update(self.comparison.vendor_names or {})
        rows = []
        total = 0.0
        by_vendor: dict[str, float] = {}
        for line in self.rfx.line_items:
            vid = self.awards.get(line.line_id, "")
            unit = price_map.get((line.line_id, vid)) if vid else None
            ext = (unit * line.qty) if unit is not None else None
            if ext is not None:
                total += ext
                by_vendor[vid] = by_vendor.get(vid, 0.0) + ext
            rows.append(
                {
                    "line_id": line.line_id,
                    "description": line.description,
                    "qty": line.qty,
                    "vendor_id": vid,
                    "vendor_name": names.get(vid, vid),
                    "unit_price_inr": unit,
                    "extended_inr": ext,
                }
            )
        return {
            "lines": rows,
            "total_inr": total,
            "by_vendor": {
                vid: {"name": names.get(vid, vid), "extended_inr": amt}
                for vid, amt in by_vendor.items()
            },
        }

    # ── Wizard navigation ──────────────────────────────────────────────

    def export_award(self, fmt: str = "csv") -> bytes | str:
        """Export current award decision as Excel / CSV / Markdown."""
        import csv
        import io

        summary = self.award_summary()
        rows = summary.get("lines") or []
        # Ensure we have line-level rows even if only awards map is filled
        if not rows and self.rfx and self.awards and self.comparison:
            summary = self.award_summary()
            rows = summary.get("lines") or []

        headers = [
            "line_id",
            "description",
            "qty",
            "vendor_id",
            "vendor_name",
            "unit_price_inr",
            "extended_inr",
        ]

        def _row_vals(r: dict[str, Any]) -> list[Any]:
            return [
                r.get("line_id", ""),
                r.get("description", ""),
                r.get("qty", ""),
                r.get("vendor_id", ""),
                r.get("vendor_name", ""),
                r.get("unit_price_inr", ""),
                r.get("extended_inr", ""),
            ]

        fmt = (fmt or "csv").lower().lstrip(".")
        if fmt in {"xlsx", "excel"}:
            from openpyxl import Workbook

            wb = Workbook()
            ws = wb.active
            ws.title = "Award"
            ws.append(headers)
            for r in rows:
                if isinstance(r, dict):
                    ws.append(_row_vals(r))
            ws.append([])
            ws.append(["Total INR", summary.get("total_inr") or 0])
            ws.append(["RFx", self.rfx.rfx_id if self.rfx else ""])
            buf = io.BytesIO()
            wb.save(buf)
            return buf.getvalue()

        if fmt in {"md", "markdown"}:
            md = summary.get("markdown") or ""
            if not md:
                lines = [
                    f"# Award decision — {self.rfx.rfx_id if self.rfx else ''}",
                    "",
                    "| Line | Vendor | ₹/pc | Extended |",
                    "|---|---|---:|---:|",
                ]
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    up = r.get("unit_price_inr")
                    ext = r.get("extended_inr")
                    lines.append(
                        f"| {r.get('line_id','')} | {r.get('vendor_name') or r.get('vendor_id','')} | "
                        f"{'' if up is None else f'{up:.2f}'} | {'' if ext is None else f'{ext:.2f}'} |"
                    )
                lines.append("")
                lines.append(f"**Total:** ₹{summary.get('total_inr') or 0:,.2f}")
                md = "\n".join(lines)
            return md if isinstance(md, str) else str(md)

        # csv default
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        for r in rows:
            if isinstance(r, dict):
                writer.writerow(_row_vals(r))
        writer.writerow([])
        writer.writerow(["total_inr", summary.get("total_inr") or 0])
        return buf.getvalue().encode("utf-8")


    def completion(self) -> dict[str, bool]:
        return {
            "draft": bool(self.rfx and self.rfx.line_items),
            "send": bool(self.dispatch_log),
            "inbox": bool(self.quotes),
            "compare": bool(self.comparison),
            "ask": bool(self.chat) or self.wizard_step in ("ask", "award"),
            "award": bool(self.awards),
        }

    def unlocked_steps(self) -> list[str]:
        """All tabs are always available (kept for template/API compat)."""
        return list(WIZARD_STEPS)

    def set_wizard_step(self, step: str) -> str:
        """Switch the active tab. Never blocks — empty panels handle missing data."""
        if step not in WIZARD_STEPS:
            raise ValueError(f"Unknown step {step}")
        self.wizard_step = step
        if step == "inbox" and not self.inbox and self.dispatch_log:
            try:
                self.seed_inbox()
            except Exception:
                pass
        if step == "send" and self.rfx:
            try:
                self.refresh_cover_previews()
            except Exception:
                pass
        self._persist()
        return self.wizard_step

    def advance(self) -> str:
        """Move to the next tab in order (soft helper; no unlock gates)."""
        order = WIZARD_STEPS
        cur = self.wizard_step if self.wizard_step in order else "draft"
        idx = order.index(cur)
        if idx + 1 >= len(order):
            return self.wizard_step
        nxt = order[idx + 1]
        return self.set_wizard_step(nxt)

    # ── E2E / snapshot ─────────────────────────────────────────────────

    def run(
        self,
        brief: str,
        vendor_dir: Optional[Union[str, Path]] = None,
        skip_dispatch: bool = False,
        **draft_kwargs: Any,
    ) -> dict[str, Any]:
        self.draft(brief, **draft_kwargs)
        if not skip_dispatch:
            self.dispatch()
        self.ingest(directory=vendor_dir or VENDOR_DIR)
        self.normalize()
        self.wizard_step = "compare"
        self._persist()
        return self.snapshot()

    def run_e2e(
        self,
        brief: str,
        vendor_dir: Optional[Union[str, Path]] = None,
        questions: Optional[list[str]] = None,
        **draft_kwargs: Any,
    ) -> dict[str, Any]:
        self.run(brief, vendor_dir=vendor_dir, skip_dispatch=False, **draft_kwargs)
        try:
            self.seed_inbox(force=True)
            for msg in self.inbox:
                msg.status = "parsed"
        except Exception:
            pass
        qs = questions or [
            "What if we split award cheapest per line, but only among vendors who cleared the quality questionnaire?",
            "Give a defensible award recommendation with totals and the main risks.",
        ]
        for q in qs:
            try:
                self.ask(q)
            except Exception as exc:
                self.chat.append(
                    {
                        "question": q,
                        "answer": f"Analyst error: {exc}",
                        "data": None,
                        "caveats": [str(exc)],
                        "tool": None,
                        "model": None,
                        "raw": {"error": str(exc)},
                    }
                )
                self.step = "asked"
                self._persist()
        try:
            self.suggest_awards()
        except Exception:
            pass
        self.wizard_step = "award"
        self._persist()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "wizard_step": self.wizard_step,
            "brief": self.brief,
            "rfx": self.rfx.model_dump() if self.rfx else None,
            "dispatch_log": self.dispatch_log,
            "dispatch_paths": self.dispatch_paths,
            "cover_previews": self.cover_previews,
            "quotes": [q.model_dump() for q in self.quotes],
            "comparison": self.comparison.model_dump() if self.comparison else None,
            "chat": self.chat,
            "analyst_history": self.analyst_history,
            "inbox": [m.model_dump() for m in self.inbox],
            "awards": self.awards,
            "award_validation": self.award_validation,
        }

    def load_snapshot(self, data: dict[str, Any]) -> None:
        self.step = data.get("step", "idle")
        self.wizard_step = data.get("wizard_step") or "draft"
        self.brief = data.get("brief") or ""
        self.rfx = RFx.model_validate(data["rfx"]) if data.get("rfx") else None
        self.dispatch_log = list(data.get("dispatch_log") or [])
        self.dispatch_paths = list(data.get("dispatch_paths") or [])
        self.cover_previews = list(data.get("cover_previews") or [])
        # Back-compat: old cover_preview string
        if not self.cover_previews and data.get("cover_preview") and self.rfx:
            try:
                self.refresh_cover_previews()
            except Exception:
                pass
        self.quotes = [
            ExtractedQuote.model_validate(q) for q in data.get("quotes") or []
        ]
        self.comparison = (
            ComparisonTable.model_validate(data["comparison"])
            if data.get("comparison")
            else None
        )
        self.chat = list(data.get("chat") or [])
        self.analyst_history = list(data.get("analyst_history") or [])
        self.inbox = [InboxMessage.model_validate(m) for m in data.get("inbox") or []]
        self.awards = dict(data.get("awards") or {})
        self.award_validation = dict(data.get("award_validation") or {})

    def _persist(self) -> None:
        if not self.rfx:
            return
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        path = STORE_DIR / f"{self.rfx.rfx_id}.json"
        path.write_text(
            json.dumps(self.snapshot(), indent=2, default=str), encoding="utf-8"
        )


def run_pipeline(
    brief: str,
    vendor_dir: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return RFxPipeline().run(brief, vendor_dir=vendor_dir, **kwargs)


def load_pipeline(rfx_id: str) -> RFxPipeline:
    path = STORE_DIR / f"{rfx_id}.json"
    pipe = RFxPipeline()
    if path.exists():
        pipe.load_snapshot(json.loads(path.read_text(encoding="utf-8")))
    return pipe

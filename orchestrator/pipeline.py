"""Orchestrator — free tabbed workspace: Draft | Outbox | Inbox | Compare | Ask | Award.

Wires public agent APIs; persists snapshots under data/store/.
Navigation is free — wizard_step is the last-open tab, not a lock gate.
"""
from __future__ import annotations

import json
import logging
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
    write_award_notices,
)
from agents.analyst import shortlist_vendors
from shared_models import (
    ComparisonTable,
    ExtractedQuote,
    InboxMessage,
    LineItem,
    RFx,
)

ROOT = Path(__file__).resolve().parent.parent


def _writable_data_root() -> Path:
    """Repo data/ locally; /tmp on Vercel (deployment FS is read-only)."""
    override = os.environ.get("AERCHAIN_DATA_ROOT", "").strip()
    if override:
        root = Path(override)
    elif os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        root = Path("/tmp/aerchain-data")
    else:
        root = ROOT / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


_DATA_ROOT = _writable_data_root()
DATA_ROOT = _DATA_ROOT  # writable root (repo data/ or /tmp on Vercel)
# Fixtures ship with the deploy (read-only OK on Vercel).
VENDOR_DIR = ROOT / "data" / "vendor_responses"
STORE_DIR = _DATA_ROOT / "store"
OUTBOX_DIR = _DATA_ROOT / "outbox"
INBOX_DIR = _DATA_ROOT / "inbox"
for _d in (STORE_DIR, OUTBOX_DIR, INBOX_DIR):
    _d.mkdir(parents=True, exist_ok=True)

WIZARD_STEPS = ["draft", "send", "inbox", "compare", "ask", "award", "audit"]

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
        self.award_notice_paths: list[str] = []
        self.award_log: list[dict[str, Any]] = []
        self.freeze: Optional[dict[str, Any]] = None
        self.review_log: list[dict[str, Any]] = []
        self.partial_requests: list[dict[str, Any]] = []
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

    def _ensure_inbox_files(self) -> None:
        """Re-copy VENDOR_DIR fixtures into /tmp inbox when missing (cold-start safe).

        Snapshot metadata (quotes, inbox rows) lives in Blob; binary seed files only
        exist under /tmp and vanish on a new instance. Paths in the snapshot are
        remapped to the freshly seeded files by filename / vendor_id.
        """
        if not self.rfx:
            return
        inbox_root = self._inbox_dir()
        has_files = False
        if inbox_root.exists():
            has_files = any(
                p.is_file()
                and not p.name.startswith(".")
                and not p.name.endswith(".extract.json")
                for p in inbox_root.iterdir()
            )
        if not has_files:
            seed_inbox(inbox_dir=inbox_root, vendor_dir=VENDOR_DIR)
        by_name = {
            p.name: p
            for p in inbox_root.iterdir()
            if p.is_file() and not p.name.startswith(".")
        }
        if not self.inbox:
            return
        for msg in self.inbox:
            if msg.path and Path(msg.path).exists():
                continue
            name = Path(msg.path).name if msg.path else ""
            if name and name in by_name:
                msg.path = str(by_name[name])
                continue
            vid = (msg.vendor_id or "").lower()
            if not vid:
                continue
            for fname, fpath in by_name.items():
                if fname.endswith(".extract.json"):
                    continue
                if vid in fname.lower() or vid.replace("0", "") in fname.lower():
                    msg.path = str(fpath)
                    break

    def seed_inbox(self, *, force: bool = False) -> list[InboxMessage]:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        inbox_root = self._inbox_dir()
        self._ensure_inbox_files()
        if self.inbox and not force:
            # Cold start: metadata present but files were just re-seeded — keep msgs.
            if all(Path(m.path).exists() for m in self.inbox if m.path):
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
        self._ensure_inbox_files()
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
        # Cold start: /tmp inbox may be empty even when Blob snapshot has messages.
        self._ensure_inbox_files()
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

    def partial_candidates_for_line(self, line_id: str) -> list[dict[str, Any]]:
        """Priced vendors who are NOT fully award-eligible (need manager approval)."""
        if not self.comparison:
            return []
        qual = set(self.qualified_vendor_ids())
        names = dict(self.comparison.vendor_names or {})
        if self.rfx:
            names = {**{v.vendor_id: v.name for v in self.rfx.vendors}, **names}
        # Build gap notes from qualifications
        gap_by_vendor: dict[str, list[str]] = {}
        for q in self.comparison.qualifications or []:
            vid = getattr(q, "vendor_id", None) or (q.get("vendor_id") if isinstance(q, dict) else None)
            if not vid or vid in qual:
                continue
            reasons = list(getattr(q, "reasons", None) or (q.get("reasons") if isinstance(q, dict) else []) or [])
            for kr in getattr(q, "knockout_results", None) or []:
                passed = getattr(kr, "passed", None) if not isinstance(kr, dict) else kr.get("passed")
                qid = getattr(kr, "question_id", None) if not isinstance(kr, dict) else kr.get("question_id")
                reason = getattr(kr, "reason", None) if not isinstance(kr, dict) else kr.get("reason")
                if passed is False:
                    reasons.append(f"Failed KO {qid}" + (f": {reason}" if reason else ""))
                elif passed is None:
                    reasons.append(f"Unanswered KO {qid}")
            # de-dupe
            seen: set[str] = set()
            uniq: list[str] = []
            for r in reasons:
                s = str(r).strip()
                if s and s not in seen:
                    seen.add(s)
                    uniq.append(s)
            gap_by_vendor[str(vid)] = uniq or ["Not fully award-eligible (questionnaire / coverage)"]

        out: list[dict[str, Any]] = []
        for cell in self.comparison.cells:
            if cell.line_id != line_id:
                continue
            if cell.vendor_id in qual:
                continue
            if cell.unit_price_inr is None:
                continue
            if cell.status in ("missing",):
                continue
            gaps = list(gap_by_vendor.get(cell.vendor_id) or ["Not fully award-eligible"])
            if cell.status in ("uncertain", "uom_mismatch"):
                gaps.append(f"Price cell status: {cell.status}")
            out.append(
                {
                    "vendor_id": cell.vendor_id,
                    "name": names.get(cell.vendor_id, cell.vendor_id),
                    "unit_price_inr": cell.unit_price_inr,
                    "status": cell.status,
                    "gaps": gaps,
                }
            )
        out.sort(key=lambda r: (r["unit_price_inr"] is None, r["unit_price_inr"] or 0))
        return out

    def pending_partial_for_line(self, line_id: str) -> Optional[dict[str, Any]]:
        for req in self.partial_requests:
            if req.get("line_id") == line_id and req.get("status") == "pending":
                return req
        return None

    def partial_status_for_line(self, line_id: str) -> Optional[dict[str, Any]]:
        """Latest non-superseded partial request for a line (pending / approved / rejected)."""
        latest = None
        for req in self.partial_requests:
            if req.get("line_id") == line_id:
                latest = req
        return latest

    def request_partial_award(
        self,
        line_id: str,
        vendor_id: str,
        buyer_note: str,
    ) -> dict[str, Any]:
        if not self.comparison or not self.rfx:
            raise RuntimeError("Compare quotes before requesting a partial award.")
        if self.is_frozen:
            raise RuntimeError("Award is frozen — unfreeze before editing.")
        note = (buyer_note or "").strip()
        if len(note) < 8:
            raise RuntimeError("Buyer justification required (at least a short note).")
        line_id = str(line_id).strip()
        vendor_id = str(vendor_id).strip()
        if not line_id or not vendor_id:
            raise RuntimeError("line_id and vendor_id are required.")
        if line_id in self.awards and self.awards[line_id] == vendor_id:
            raise RuntimeError("That vendor is already awarded on this line.")
        # Must be a partial candidate (priced + not fully eligible)
        cands = {c["vendor_id"]: c for c in self.partial_candidates_for_line(line_id)}
        if vendor_id not in cands:
            # Fully eligible vendors use the normal award path
            elig = {e["vendor_id"] for e in self.eligible_vendors_for_line(line_id)}
            if vendor_id in elig:
                raise RuntimeError(
                    "Vendor is fully award-eligible — use the normal Award dropdown (no approval needed)."
                )
            raise RuntimeError("Vendor has no usable price on this line (or is missing).")
        cand = cands[vendor_id]
        # Replace any existing pending for this line
        kept: list[dict[str, Any]] = []
        for req in self.partial_requests:
            if req.get("line_id") == line_id and req.get("status") == "pending":
                continue
            kept.append(req)
        self.partial_requests = kept
        req_id = f"PA-{uuid.uuid4().hex[:8].upper()}"
        names = dict(self.comparison.vendor_names or {})
        if self.rfx:
            names = {**{v.vendor_id: v.name for v in self.rfx.vendors}, **names}
        entry = {
            "request_id": req_id,
            "line_id": line_id,
            "vendor_id": vendor_id,
            "vendor_name": names.get(vendor_id, cand.get("name") or vendor_id),
            "unit_price_inr": cand.get("unit_price_inr"),
            "gaps": list(cand.get("gaps") or []),
            "buyer_note": note,
            "status": "pending",
            "manager_comment": "",
            "requested_at": self._now_iso(),
            "resolved_at": "",
            "provisional": True,
        }
        self.partial_requests.append(entry)
        gap_txt = "; ".join(entry["gaps"][:3]) if entry["gaps"] else "partial qualification"
        self.append_review_log(
            "partial_award_requested",
            f"{req_id} · {line_id} → {vendor_id} · {gap_txt} · note: {note[:120]}",
            actor="buyer",
            persist=False,
        )
        self.wizard_step = "award"
        self._persist()
        return entry

    def approve_partial_award(
        self,
        request_id: str,
        manager_comment: str = "",
    ) -> dict[str, Any]:
        if self.is_frozen:
            raise RuntimeError("Award is frozen — unfreeze before approving.")
        req = next((r for r in self.partial_requests if r.get("request_id") == request_id), None)
        if not req:
            raise RuntimeError("Partial award request not found.")
        if req.get("status") != "pending":
            raise RuntimeError(f"Request is already {req.get('status')}.")
        req["status"] = "approved"
        req["manager_comment"] = (manager_comment or "").strip()
        req["resolved_at"] = self._now_iso()
        req["provisional"] = False
        # Promote into firm awards (bypass normal qual gate — manager approved)
        lid = str(req["line_id"])
        vid = str(req["vendor_id"])
        self.awards[lid] = vid
        # Refresh validation display without rejecting this line
        try:
            # Keep other awards; re-validate only firm eligible ones, then re-add approved partials
            firm = {
                k: v
                for k, v in self.awards.items()
                if not any(
                    r.get("line_id") == k
                    and r.get("vendor_id") == v
                    and r.get("status") == "approved"
                    for r in self.partial_requests
                )
            }
            # Actually all approved partials are in awards; validate may reject them.
            # Store a lightweight award_validation patch instead.
            result = validate_award(
                self.comparison,
                qualifications=self.qualified_vendor_ids(),
                awards={k: v for k, v in self.awards.items() if k != lid},
                rfx=self.rfx,
            )
            # Merge approved partials back into awards map (already set)
            # Annotate validation so UI totals can include them
            lines = list(result.get("awards") or [])
            price = req.get("unit_price_inr")
            qty = 1.0
            if self.rfx:
                for li in self.rfx.line_items:
                    if li.line_id == lid:
                        qty = float(li.qty or 1)
                        break
            ext = float(price or 0) * qty
            lines.append(
                {
                    "line_id": lid,
                    "vendor_id": vid,
                    "unit_price_inr": price,
                    "extended_inr": ext,
                    "partial_approved": True,
                }
            )
            result["awards"] = lines
            totals = dict(result.get("totals") or {})
            gt = float(totals.get("grand_total_inr") or totals.get("grand_total_partial_inr") or 0)
            totals["grand_total_inr"] = gt + ext
            totals["grand_total_partial_inr"] = totals["grand_total_inr"]
            result["totals"] = totals
            self.award_validation = result
        except Exception:
            pass
        detail = f"{request_id} · {lid} → {vid}"
        if manager_comment:
            detail = f"{detail} — {manager_comment.strip()[:120]}"
        self.append_review_log(
            "partial_award_approved",
            detail,
            actor="manager",
            persist=False,
        )
        self.wizard_step = "award"
        self._persist()
        return req

    def reject_partial_award(
        self,
        request_id: str,
        manager_comment: str = "",
    ) -> dict[str, Any]:
        if self.is_frozen:
            raise RuntimeError("Award is frozen — unfreeze before rejecting.")
        req = next((r for r in self.partial_requests if r.get("request_id") == request_id), None)
        if not req:
            raise RuntimeError("Partial award request not found.")
        if req.get("status") != "pending":
            raise RuntimeError(f"Request is already {req.get('status')}.")
        req["status"] = "rejected"
        req["manager_comment"] = (manager_comment or "").strip()
        req["resolved_at"] = self._now_iso()
        req["provisional"] = False
        # Ensure not sitting in firm awards
        lid = str(req["line_id"])
        if self.awards.get(lid) == req.get("vendor_id"):
            self.awards.pop(lid, None)
        detail = f"{request_id} · {lid} → {req.get('vendor_id')}"
        if manager_comment:
            detail = f"{detail} — {manager_comment.strip()[:120]}"
        self.append_review_log(
            "partial_award_rejected",
            detail,
            actor="manager",
            persist=False,
        )
        self.wizard_step = "award"
        self._persist()
        return req

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def append_review_log(
        self,
        action: str,
        detail: str = "",
        *,
        actor: str = "buyer",
        persist: bool = True,
    ) -> dict[str, Any]:
        """Append-only assignment-trust event (freeze / save / notices / override)."""
        entry = {
            "time": self._now_iso(),
            "action": action,
            "detail": detail or "",
            "actor": actor or "buyer",
        }
        self.review_log.append(entry)
        if persist:
            self._persist()
        return entry

    @property
    def is_frozen(self) -> bool:
        return bool(self.freeze)

    def freeze_award(self, note: str = "") -> dict[str, Any]:
        """Snapshot awards + shortlist Pass vendors; lock dropdowns until unfreeze."""
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        if not self.awards:
            raise RuntimeError("Save awards before freezing.")
        if self.freeze:
            raise RuntimeError("Award is already frozen.")
        pending = [r for r in self.partial_requests if r.get("status") == "pending"]
        if pending:
            raise RuntimeError(
                f"{len(pending)} partial award request(s) still pending manager approval — "
                "resolve them before freezing."
            )
        summary = self.award_summary()
        pass_vendors: list[dict[str, Any]] = []
        try:
            for row in self.shortlist():
                if row.get("pass"):
                    pass_vendors.append(
                        {
                            "vendor_id": row.get("vendor_id"),
                            "name": row.get("name"),
                            "coverage": row.get("coverage"),
                        }
                    )
        except Exception:
            pass_vendors = []
        freeze_id = f"FZ-{uuid.uuid4().hex[:8].upper()}"
        snap = {
            "freeze_id": freeze_id,
            "rfx_id": self.rfx.rfx_id,
            "awards": dict(self.awards),
            "totals": {
                "total_inr": summary.get("total_inr") or 0.0,
                "by_vendor": summary.get("by_vendor") or {},
                "lines_awarded": len(self.awards),
            },
            "timestamp": self._now_iso(),
            "shortlist_pass": pass_vendors,
            "comparison_version": {
                "wizard_step": self.wizard_step,
                "step": self.step,
                "cell_count": len(self.comparison.cells) if self.comparison else 0,
                "eligible": list(self.qualified_vendor_ids()),
            },
            "note": (note or "").strip(),
        }
        self.freeze = snap
        detail = f"{freeze_id} · {len(self.awards)} lines · ₹{summary.get('total_inr') or 0:.2f}"
        if note:
            detail = f"{detail} — {note.strip()}"
        self.append_review_log("freeze", detail, persist=False)
        self.wizard_step = "award"
        self._persist()
        return snap

    def unfreeze_award(self, note: str = "") -> None:
        if not self.freeze:
            raise RuntimeError("Award is not frozen.")
        fid = self.freeze.get("freeze_id", "")
        detail = f"Unfroze {fid}".strip()
        if note:
            detail = f"{detail} — {note.strip()}"
        self.freeze = None
        self.append_review_log("unfreeze", detail, persist=False)
        self.wizard_step = "award"
        self._persist()

    def log_cell_override(
        self,
        line_id: str,
        vendor_id: str,
        note: str,
        *,
        status_note: str = "",
    ) -> dict[str, Any]:
        """Cheap override: review_log only (cell model has no reviewed status)."""
        note = (note or "").strip()
        if not note:
            raise RuntimeError("Override requires a note.")
        detail = f"{line_id}/{vendor_id}: {note}"
        if status_note:
            detail = f"{detail} ({status_note})"
        return self.append_review_log("cell_override", detail)

    def save_awards(self, awards: dict[str, str]) -> dict[str, Any]:
        if not self.comparison or not self.rfx:
            raise RuntimeError("Compare quotes before awarding.")
        if self.is_frozen:
            raise RuntimeError("Award is frozen — unfreeze before editing.")
        # Approved partials may be submitted even though vendor is not fully eligible.
        approved_map = {
            str(r.get("line_id")): str(r.get("vendor_id"))
            for r in self.partial_requests
            if r.get("status") == "approved" and r.get("line_id") and r.get("vendor_id")
        }
        # Split: fully-eligible awards vs approved-partial lines
        firm_input: dict[str, str] = {}
        partial_keep: dict[str, str] = {}
        qual = set(self.qualified_vendor_ids())
        for lid, vid in (awards or {}).items():
            lid, vid = str(lid), str(vid)
            if not lid or not vid:
                continue
            if vid in qual:
                firm_input[lid] = vid
            elif approved_map.get(lid) == vid:
                partial_keep[lid] = vid
            elif lid in approved_map and approved_map[lid] != vid:
                # Buyer reassigned away from approved partial — drop that approval link
                firm_input[lid] = vid  # may still be rejected by validate if not qual
            else:
                # Not eligible and not approved — let validate reject
                firm_input[lid] = vid
        result = validate_award(
            self.comparison,
            qualifications=self.qualified_vendor_ids(),
            awards=firm_input,
            rfx=self.rfx,
        )
        cleaned: dict[str, str] = {}
        for row in result.get("awards") or []:
            lid = str(row.get("line_id") or "")
            vid = str(row.get("vendor_id") or "")
            if lid and vid:
                cleaned[lid] = vid
        # Re-apply approved partials the buyer still wants
        for lid, vid in partial_keep.items():
            cleaned[lid] = vid
        # If buyer cleared a line that had approved partial (not in awards), drop from cleaned
        # (already absent). Mark those requests as superseded? leave history as approved.
        self.awards = cleaned
        self.award_validation = result
        self.step = "awarded"
        self.wizard_step = "award"
        n = len(cleaned)
        total = (result.get("totals") or {}).get("grand_total_inr") or (
            result.get("totals") or {}
        ).get("grand_total_partial_inr") or 0.0
        # Add partial approved extended values roughly
        if self.rfx:
            for lid, vid in partial_keep.items():
                for req in self.partial_requests:
                    if req.get("line_id") == lid and req.get("vendor_id") == vid and req.get("status") == "approved":
                        qty = 1.0
                        for li in self.rfx.line_items:
                            if li.line_id == lid:
                                qty = float(li.qty or 1)
                                break
                        total = float(total) + float(req.get("unit_price_inr") or 0) * qty
                        break
        self.append_review_log(
            "award_saved",
            f"{n} line(s) · ₹{float(total):.2f}",
            persist=False,
        )
        self._persist()
        return result

    def suggest_awards(self) -> dict[str, Any]:
        if not self.comparison:
            raise RuntimeError("Normalize first.")
        if self.is_frozen:
            raise RuntimeError("Award is frozen — unfreeze before editing.")
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
        n = len(cleaned)
        total = (result.get("totals") or {}).get("grand_total_inr") or (
            result.get("totals") or {}
        ).get("grand_total_partial_inr") or 0.0
        self.append_review_log(
            "award_saved",
            f"suggest cheapest split · {n} line(s) · ₹{float(total):.2f}",
            persist=False,
        )
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
            "ask": bool(self.chat) or self.wizard_step in ("ask", "award", "audit"),
            "award": bool(self.awards),
            "audit": bool(self.review_log),
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


    def provisional_status(self) -> dict[str, Any]:
        """Amber banner state when inbox/quotes are incomplete vs expected vendors."""
        expected = 5
        if self.rfx and self.rfx.vendors:
            expected = max(1, len(self.rfx.vendors))
        inbox = list(self.inbox or [])
        quotes = list(self.quotes or [])
        pending_msgs = [
            m for m in inbox
            if getattr(m, "status", "new") in ("new", "error")
            or str(getattr(m, "status", "")) in ("new", "error", "parsing")
        ]
        parsed_msgs = [m for m in inbox if getattr(m, "status", "") == "parsed"]
        vendor_ids_quoted = {getattr(q, "vendor_id", "") for q in quotes if getattr(q, "vendor_id", "")}
        still_extracting = max(0, expected - len(vendor_ids_quoted))
        # Prefer unparsed inbox count when seeded messages exist
        if inbox:
            still_extracting = max(still_extracting, len(pending_msgs))
        active = bool(inbox or quotes) and (
            bool(pending_msgs) or len(vendor_ids_quoted) < expected
        )
        if not inbox and not quotes:
            active = False
        msg = ""
        if active:
            n = still_extracting or len(pending_msgs) or (expected - len(vendor_ids_quoted))
            n = max(1, n)
            msg = f"Provisional — {n} vendor response{'s' if n != 1 else ''} still extracting"
        return {
            "active": active,
            "message": msg,
            "pending_messages": len(pending_msgs),
            "parsed_messages": len(parsed_msgs),
            "quotes": len(quotes),
            "expected_vendors": expected,
            "vendors_quoted": len(vendor_ids_quoted),
            "still_extracting": still_extracting,
        }

    def shortlist(self) -> list[dict[str, Any]]:
        """Pass-first shortlist rows for Compare/Award chips."""
        if not self.comparison:
            return []
        try:
            return shortlist_vendors(self.comparison, rfx=self.rfx)
        except Exception:
            return []

    def evidence_for_cell(self, line_id: str, vendor_id: str) -> dict[str, Any]:
        """Assemble evidence drawer payload for one comparison cell."""
        cell = None
        if self.comparison:
            for c in self.comparison.cells:
                if c.line_id == line_id and c.vendor_id == vendor_id:
                    cell = c
                    break
        quote = next((q for q in (self.quotes or []) if q.vendor_id == vendor_id), None)
        name = vendor_id
        if self.rfx:
            for v in self.rfx.vendors:
                if v.vendor_id == vendor_id:
                    name = v.name
                    break
        if self.comparison and self.comparison.vendor_names:
            name = self.comparison.vendor_names.get(vendor_id, name)

        snippets: list[dict[str, str]] = []
        source_file = ""
        if quote:
            for item in quote.raw_evidence or []:
                if not isinstance(item, dict):
                    continue
                if item.get("kind") == "meta":
                    source_file = str(item.get("source_file") or source_file)
                    continue
                sn = str(item.get("snippet") or item.get("text") or "").strip()
                if not sn:
                    continue
                # Prefer snippets that mention this line id; keep a few general ones
                loc = str(item.get("location") or "")
                snippets.append({"snippet": sn, "location": loc})
            # Rank: line_id mentions first
            lid = str(line_id).lower()
            snippets.sort(
                key=lambda s: (0 if lid and lid in s["snippet"].lower() else 1)
            )
            snippets = snippets[:8]
            if not source_file:
                source_file = str(getattr(quote, "source_file", "") or "")

        if not source_file:
            # A missing cell can still have an inbox file worth showing.
            for msg in (self.inbox or []):
                msg_vid = str(getattr(msg, "parsed_vendor_id", "") or getattr(msg, "vendor_id", "") or "")
                if msg_vid == vendor_id:
                    source_file = str(getattr(msg, "path", "") or "")
                    if source_file:
                        break
        if not source_file and quote:
            source_file = str(getattr(quote, "source_format", "") or "")

        line_desc = ""
        if self.rfx:
            for li in self.rfx.line_items:
                if li.line_id == line_id:
                    line_desc = li.description
                    break

        status = cell.status if cell else "missing"
        flags = list(cell.flags) if cell else []
        fx_uom_note = "; ".join(flags) if flags else ""
        if cell and cell.status == "converted" and cell.original_price is not None:
            bit = f"Original {cell.original_currency or '?'} {cell.original_price}"
            if cell.original_uom:
                bit += f" / {cell.original_uom}"
            fx_uom_note = f"{bit}. {fx_uom_note}".strip(". ")

        return {
            "line_id": line_id,
            "line_description": line_desc,
            "vendor_id": vendor_id,
            "vendor_name": name,
            "status": status,
            "unit_price_inr": cell.unit_price_inr if cell else None,
            "original_price": cell.original_price if cell else None,
            "original_currency": cell.original_currency if cell else None,
            "original_uom": cell.original_uom if cell else None,
            "flags": flags,
            "fx_uom_note": fx_uom_note,
            "source_file": source_file,
            "snippets": snippets,
            "notes": getattr(quote, "notes", "") if quote else "",
            "confidence": getattr(quote, "confidence", None) if quote else None,
        }

    def notify_awarded_vendors(self) -> list[str]:
        """Stub-write award_notice_*.txt into data/outbox; surface full emails on Outbox."""
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        if not self.awards:
            raise RuntimeError("Save awards before sending notices.")
        lines = None
        if self.award_validation:
            lines = self.award_validation.get("awards")
        agent = VendorDispatcherAgent(stub=True, outbox_dir=OUTBOX_DIR)
        paths = agent.write_award_notices(self.rfx, self.awards, lines=lines)
        # Agent.dispatch_log holds full award_notice rows (to/subject/body_preview).
        records = [dict(r) for r in (agent.dispatch_log or []) if r.get("kind") == "award_notice"]
        for r in records:
            r["status"] = "stub_sent"
            r["delivery"] = "stub_sent"
            r.setdefault("file", Path(str(r.get("path") or "")).name)
        self.award_notice_paths = [str(p) for p in paths]
        self.award_log = records
        # Replace prior award_notice rows on Outbox log, keep RFQ invites.
        kept: list[dict[str, Any]] = []
        for row in self.dispatch_log or []:
            kind = str(row.get("kind") or "")
            path_s = str(row.get("path") or row.get("file") or "").lower()
            subj = str(row.get("subject") or "").lower()
            if kind == "award_notice" or "award_notice" in path_s or "award notice" in subj:
                continue
            kept.append(row)
        kept.extend(records)
        self.dispatch_log = kept
        names = ", ".join(Path(p).name for p in self.award_notice_paths) or "(none)"
        self.append_review_log(
            "award_notices_sent",
            f"{len(self.award_notice_paths)} notice(s) stub_sent: {names}",
            persist=False,
        )
        self._persist()
        return self.award_notice_paths


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
            "award_notice_paths": list(self.award_notice_paths or []),
            "award_log": list(self.award_log or []),
            "freeze": self.freeze,
            "review_log": list(self.review_log or []),
            "partial_requests": list(self.partial_requests or []),
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
        self.award_notice_paths = list(data.get("award_notice_paths") or [])
        self.award_log = list(data.get("award_log") or [])
        self.freeze = data.get("freeze") or None
        self.review_log = list(data.get("review_log") or [])
        self.partial_requests = list(data.get("partial_requests") or [])

    def _persist(self) -> None:
        """Write snapshot to local STORE_DIR (best effort) and Blob when configured.

        On Vercel, /tmp is instance-local. When BLOB_READ_WRITE_TOKEN is set the
        pipeline JSON (quotes, comparison, awards, inbox metadata, dispatch_log)
        is also written via core.storage.save_state so cold starts can reload.
        """
        if not self.rfx:
            return
        snap = self.snapshot()
        payload = json.dumps(snap, indent=2, default=str)
        fallback = Path("/tmp/aerchain-data/store")
        candidates: list[Path] = [STORE_DIR]
        if fallback.resolve() != STORE_DIR.resolve():
            candidates.append(fallback)
        local_ok = False
        last_err: Optional[OSError] = None
        for store in candidates:
            try:
                store.mkdir(parents=True, exist_ok=True)
                (store / f"{self.rfx.rfx_id}.json").write_text(payload, encoding="utf-8")
                local_ok = True
                break
            except OSError as exc:
                last_err = exc
                logging.getLogger(__name__).warning(
                    "persist to %s failed (%s); trying next store", store, exc
                )

        blob_ok = False
        try:
            from core import storage as _storage

            if os.environ.get("BLOB_READ_WRITE_TOKEN") or _storage.backend_name() == "vercel-blob":
                # Copy so storage.save_state can attach _version without mutating snap.
                _storage.save_state(self.rfx.rfx_id, dict(snap))
                blob_ok = True
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "blob save_state(%s) failed: %s", self.rfx.rfx_id, exc
            )

        if not local_ok and not blob_ok and last_err:
            raise last_err


def run_pipeline(
    brief: str,
    vendor_dir: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return RFxPipeline().run(brief, vendor_dir=vendor_dir, **kwargs)


def load_pipeline(rfx_id: str) -> RFxPipeline:
    """Load from local STORE_DIR (/tmp on Vercel), then Blob via core.storage.

    Cold starts wipe in-memory _SESSIONS and instance-local /tmp. When
    BLOB_READ_WRITE_TOKEN is set, snapshots dual-written by _persist are
    recovered through core.storage.load_state.
    """
    pipe = RFxPipeline()
    fallback = Path("/tmp/aerchain-data/store") / f"{rfx_id}.json"
    candidates = [STORE_DIR / f"{rfx_id}.json"]
    if fallback not in candidates:
        candidates.append(fallback)
    for path in candidates:
        try:
            if path.exists():
                pipe.load_snapshot(json.loads(path.read_text(encoding="utf-8")))
                return pipe
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "load_pipeline(%s) from %s failed: %s", rfx_id, path, exc
            )
            continue

    try:
        from core import storage as _storage

        data = _storage.load_state(rfx_id)
        if data:
            pipe.load_snapshot(data)
            return pipe
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "load_pipeline(%s) from storage failed: %s", rfx_id, exc
        )
    return pipe

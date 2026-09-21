"""Orchestrator — free tabbed workspace: Draft | Outbox | Inbox | Compare | Ask | Award.

Wires public agent APIs; persists snapshots under data/store/.
Navigation is free — wizard_step is the last-open tab, not a lock gate.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from agents.analyst import ask_question, explain_award as analyst_explain_award, suggest_split_award, validate_award
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



def _source_kind_from_name(name: str) -> str:
    """Map a filename/extension to a coarse source_kind for the evidence drawer."""
    ext = Path(str(name or "")).suffix.lower().lstrip(".")
    mapping = {
        "pdf": "pdf",
        "png": "image",
        "jpg": "image",
        "jpeg": "image",
        "gif": "image",
        "webp": "image",
        "bmp": "image",
        "tif": "image",
        "tiff": "image",
        "eml": "email",
        "txt": "text",
        "md": "text",
        "xlsx": "xlsx",
        "xls": "xlsx",
        "docx": "docx",
        "doc": "docx",
        "json": "json",
        "csv": "csv",
    }
    return mapping.get(ext, "unknown")


def _read_preview_text(path: Path, limit: int = 4096) -> str:
    """Safe-decode the first ~limit bytes of a text-like vendor artifact."""
    try:
        raw = path.read_bytes()[: max(512, int(limit))]
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    # Drop NULs that break HTML <pre>
    return text.replace("\x00", "")


def _looks_like_email(text: str) -> bool:
    head = (text or "")[:800].lstrip()
    return bool(re.match(r"(?i)^from:\s*\S", head))


_TEXT_LIKE_EXTS = {".txt", ".eml", ".md", ".csv", ".json"}
_BUYER_TO = "procurement@company.com"


def _fmt_money(val: Any) -> str:
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val):,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _answer_id(row: dict[str, Any]) -> str:
    return str(
        row.get("id")
        or row.get("question_id")
        or row.get("qid")
        or ""
    ).strip()


def _body_from_quote(
    quote: ExtractedQuote,
    *,
    vendor_name: str,
    rfx_id: str,
) -> str:
    """Plain-text vendor reply built from the same ExtractedQuote Compare uses."""
    line_rows: list[str] = []
    for ln in quote.lines or []:
        if not isinstance(ln, dict):
            continue
        lid = str(ln.get("line_id") or "").strip() or "?"
        desc = str(ln.get("description") or "").strip()
        price = _fmt_money(ln.get("unit_price") if ln.get("unit_price") is not None else ln.get("unit_price_inr"))
        cur = str(ln.get("currency") or "INR").strip() or "INR"
        uom = str(ln.get("uom") or "piece").strip() or "piece"
        desc_bit = f"  {desc}" if desc else ""
        line_rows.append(f"  {lid:<6}{desc_bit}  →  {price} {cur} / {uom}")
    lines_block = "\n".join(line_rows) if line_rows else "  (no priced lines extracted)"

    ans_rows: list[str] = []
    for row in quote.questionnaire_answers or []:
        if not isinstance(row, dict):
            continue
        qid = _answer_id(row) or "?"
        answer = str(row.get("answer") or "").strip() or "(blank)"
        ans_rows.append(f"  {qid}: {answer}")
    answers_block = "\n".join(ans_rows) if ans_rows else "  (no questionnaire answers extracted)"

    notes = str(quote.notes or "").strip()
    notes_block = notes if notes else "(none)"

    name = vendor_name or quote.vendor_id or "Vendor"
    rid = rfx_id or "RFx"
    return (
        f"Dear Procurement Team,\n\n"
        f"Thanks for the RFx {rid}. Here is our offer based on the package we reviewed.\n\n"
        f"── LINE PRICES ──────────────────────────────────────────────────────────\n"
        f"{lines_block}\n\n"
        f"── QUESTIONNAIRE ANSWERS ────────────────────────────────────────────────\n"
        f"{answers_block}\n\n"
        f"── COMMERCIAL NOTES ────────────────────────────────────────────────────\n"
        f"{notes_block}\n\n"
        f"Happy to clarify any line or commercial term.\n\n"
        f"Regards,\n"
        f"{name}\n"
    )


def _body_stub_attachment(
    *,
    filename: str,
    vendor_name: str,
    rfx_id: str,
) -> str:
    name = vendor_name or "Vendor"
    rid = rfx_id or "RFx"
    fname = filename or "quote.bin"
    return (
        f"Dear Procurement Team,\n\n"
        f"Thanks for the RFx {rid}. Please find our quotation attached.\n\n"
        f"Attachment: {fname}\n\n"
        f"(Open Inbox → Parse all to extract line prices and questionnaire answers "
        f"into Compare. Stub replies mirror what Parse puts into Compare.)\n\n"
        f"Regards,\n"
        f"{name}\n"
    )


def _source_format_of(path: str, fallback: str = "") -> str:
    ext = Path(str(path or "")).suffix.lower().lstrip(".")
    if ext:
        return ext
    fb = str(fallback or "").strip().lower()
    return fb or "unknown"


def _msg_date_label(path: str) -> str:
    try:
        p = Path(path)
        if p.is_file():
            ts = datetime.fromtimestamp(p.stat().st_mtime)
            return ts.strftime("%a, %d %b %Y %H:%M")
    except OSError:
        pass
    return datetime.now().strftime("%a, %d %b %Y %H:%M")


def inbox_email_cards(pipe: "RFxPipeline") -> list[dict[str, Any]]:
    """Build inbound email cards aligned with parsed quotes (Compare source of truth).

    Prefer quote-derived bodies after Parse so Inbox cannot drift from matrix cells.
    Before parse: show text/eml file contents, or a short attachment stub for binaries.
    """
    rfx = getattr(pipe, "rfx", None)
    rfx_id = getattr(rfx, "rfx_id", "") if rfx else ""
    vendor_by_id: dict[str, Any] = {}
    if rfx:
        for v in rfx.vendors or []:
            vendor_by_id[v.vendor_id] = v

    quotes_by_vid: dict[str, ExtractedQuote] = {}
    for q in getattr(pipe, "quotes", None) or []:
        if q and getattr(q, "vendor_id", None):
            quotes_by_vid[str(q.vendor_id)] = q

    cards: list[dict[str, Any]] = []
    seen_vids: set[str] = set()

    for msg in getattr(pipe, "inbox", None) or []:
        vid = str(
            getattr(msg, "parsed_vendor_id", "")
            or getattr(msg, "vendor_id", "")
            or ""
        )
        vendor = vendor_by_id.get(vid)
        from_name = (
            getattr(msg, "vendor_name", "")
            or (vendor.name if vendor else "")
            or vid
            or "Vendor"
        )
        from_email = (
            getattr(msg, "from_addr", "")
            or (vendor.email if vendor else "")
            or "quotes@vendor.example"
        )
        subject = getattr(msg, "subject", "") or (
            f"Re: RFx {rfx_id} — quotation" if rfx_id else "Vendor quotation"
        )
        path_s = str(getattr(msg, "path", "") or "")
        path = Path(path_s) if path_s else None
        fname = path.name if path else ""
        quote = quotes_by_vid.get(vid)
        source_format = _source_format_of(
            path_s, getattr(quote, "source_format", "") if quote else ""
        )
        status = str(getattr(msg, "status", "") or "new")

        if quote:
            body = _body_from_quote(quote, vendor_name=from_name, rfx_id=rfx_id)
            if status == "new":
                status = "parsed"
        else:
            body = ""
            ext = (path.suffix.lower() if path else "")
            if path and path.is_file() and (
                ext in _TEXT_LIKE_EXTS or ext == ""
            ):
                text = _read_preview_text(path, limit=12000)
                if text.strip() and (
                    ext in {".txt", ".eml", ".md"} or _looks_like_email(text)
                ):
                    body = text.strip()
                elif text.strip() and ext in {".csv", ".json"}:
                    # Still text-like; show a short head so the card isn't empty.
                    body = text.strip()[:4000]
            if not body:
                body = _body_stub_attachment(
                    filename=fname or "(attachment)",
                    vendor_name=from_name,
                    rfx_id=rfx_id,
                )

        cards.append(
            {
                "from_name": from_name,
                "from_email": from_email,
                "to": _BUYER_TO,
                "subject": subject,
                "date": _msg_date_label(path_s),
                "vendor_id": vid,
                "msg_id": getattr(msg, "msg_id", "") or "",
                "source_format": source_format,
                "status": status,
                "body": body,
                "path": path_s,
                "filename": fname,
                "aligned": bool(quote),
            }
        )
        if vid:
            seen_vids.add(vid)

    # Quotes without an inbox row (e.g. direct ingest) still get a readable card.
    for vid, quote in quotes_by_vid.items():
        if vid in seen_vids:
            continue
        vendor = vendor_by_id.get(vid)
        from_name = (vendor.name if vendor else "") or vid
        from_email = (vendor.email if vendor else "") or f"{vid.lower()}@vendor.example"
        body = _body_from_quote(quote, vendor_name=from_name, rfx_id=rfx_id)
        cards.append(
            {
                "from_name": from_name,
                "from_email": from_email,
                "to": _BUYER_TO,
                "subject": f"Re: RFx {rfx_id} — quotation" if rfx_id else "Vendor quotation",
                "date": datetime.now().strftime("%a, %d %b %Y %H:%M"),
                "vendor_id": vid,
                "msg_id": "",
                "source_format": str(quote.source_format or "unknown"),
                "status": "parsed",
                "body": body,
                "path": "",
                "filename": "",
                "aligned": True,
            }
        )

    return cards


def write_inbox_reply_eml_files(pipe: "RFxPipeline") -> list[str]:
    """Optional: persist quote-aligned bodies as vendor_reply_<id>.eml.txt in the RFX inbox."""
    written: list[str] = []
    try:
        inbox_root = pipe._inbox_dir()
    except Exception:
        return written
    inbox_root.mkdir(parents=True, exist_ok=True)
    for card in inbox_email_cards(pipe):
        if not card.get("aligned"):
            continue
        vid = str(card.get("vendor_id") or "").strip()
        if not vid:
            continue
        dest = inbox_root / f"vendor_reply_{vid}.eml.txt"
        header = (
            f"From: {card.get('from_name')} <{card.get('from_email')}>\n"
            f"To: {card.get('to')}\n"
            f"Subject: {card.get('subject')}\n"
            f"Date: {card.get('date')}\n"
            f"\n"
        )
        try:
            dest.write_text(header + str(card.get("body") or ""), encoding="utf-8")
            written.append(str(dest))
        except OSError:
            continue
    return written



def _stable_msg_id(path: Union[str, Path]) -> str:
    """Deterministic id from filename so re-seed / reload keeps Parse buttons working."""
    name = Path(path).name.lower()
    return "msg-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]

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
        self.manager_award_notice: Optional[dict[str, Any]] = None
        self.freeze: Optional[dict[str, Any]] = None
        self.review_log: list[dict[str, Any]] = []
        self.partial_requests: list[dict[str, Any]] = []
        self.suggested_awards: dict[str, str] = {}
        self.award_explanation: Optional[str] = None
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
            # Keep scope empty until Generate fills it with the drafted package
            scope=scope.strip() if scope and scope.strip() else "",
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
                if not getattr(msg, "filename", None):
                    msg.filename = name
                continue
            vid = (msg.vendor_id or "").lower()
            if not vid:
                continue
            for fname, fpath in by_name.items():
                if fname.endswith(".extract.json"):
                    continue
                if vid in fname.lower() or vid.replace("0", "") in fname.lower():
                    msg.path = str(fpath)
                    if not getattr(msg, "filename", None):
                        msg.filename = fname
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
                    msg_id=_stable_msg_id(path),
                    vendor_id=vid,
                    vendor_name=vendor.name if vendor else path.stem,
                    subject=f"Re: RFx {self.rfx.rfx_id} — quotation",
                    from_addr=vendor.email if vendor else f"quotes@{path.stem.lower()}.example",
                    path=str(path),
                    filename=path.name,
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
            msg_id=_stable_msg_id(dest),
            vendor_id=vendor_id,
            vendor_name=vendor.name if vendor else dest.stem,
            subject=f"Upload — {dest.name}",
            from_addr=vendor.email if vendor else "upload@local",
            path=str(dest),
            filename=dest.name,
            body_preview=preview.replace("\n", " "),
            status="new",
        )
        self.inbox.append(msg)
        self._persist()
        return msg

    def parse_inbox_message(self, msg_id: str, *, filename: str = "") -> ExtractedQuote:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        self._ensure_inbox_files()
        msg = next((m for m in self.inbox if m.msg_id == msg_id), None)
        if not msg and filename:
            fname = Path(filename).name
            msg = next(
                (m for m in self.inbox if Path(m.path or "").name == fname),
                None,
            )
        if not msg and msg_id:
            # Stale UI after re-seed: map old id → same stable filename hash if present
            msg = next((m for m in self.inbox if m.msg_id == msg_id), None)
        if not msg:
            # Last resort: if inbox empty, re-seed once then retry by filename/id
            if not self.inbox:
                self.seed_inbox(force=True)
                msg = next((m for m in self.inbox if m.msg_id == msg_id), None)
                if not msg and filename:
                    fname = Path(filename).name
                    msg = next(
                        (m for m in self.inbox if Path(m.path or "").name == fname),
                        None,
                    )
        if not msg:
            known = ", ".join(m.msg_id for m in self.inbox[:8]) or "(empty inbox)"
            raise KeyError(
                f"Unknown inbox message {msg_id}. Refresh Inbox and try again. Known: {known}"
            )
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
            try:
                write_inbox_reply_eml_files(self)
            except Exception:
                logging.getLogger(__name__).debug(
                    "write_inbox_reply_eml_files failed", exc_info=True
                )
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
                label = getattr(msg, "filename", None) or Path(getattr(msg, "path", "") or "").name or msg.msg_id
                errors.append(f"{label}: {exc}")
        if not quotes and errors:
            raise RuntimeError("; ".join(errors[:3]))
        self.quotes = quotes
        self.step = "parsed"
        self.wizard_step = "inbox"
        try:
            write_inbox_reply_eml_files(self)
        except Exception:
            logging.getLogger(__name__).debug(
                "write_inbox_reply_eml_files failed", exc_info=True
            )
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
            turn = {
                "question": question,
                "answer": (
                    "I don't have a comparison matrix loaded yet. "
                    "Open Compare → Build comparison matrix (after Inbox Parse all), "
                    "then ask again — I'll ground the answer on live prices and knockouts."
                ),
                "data": None,
                "artifacts": [],
                "caveats": ["comparison_missing"],
                "tool": "guard",
                "model": None,
                "raw": {},
            }
            self.chat.append(turn)
            self.wizard_step = "ask"
            self._persist()
            return turn
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
            "artifacts": result.get("artifacts") or [],
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


    def ensure_auto_awards(self) -> bool:
        """Pre-fill Award with cheapest Pass split when empty. Returns True if ran."""
        if self.is_frozen:
            return False
        if not self.comparison or not self.rfx:
            return False
        if self.awards:
            # Still capture baseline if missing (legacy sessions)
            if not self.suggested_awards:
                self.suggested_awards = dict(self.awards)
                self._persist()
            return False
        try:
            self.suggest_awards()
            return True
        except Exception:
            return False

    def firm_awards_map(self) -> dict[str, str]:
        """Awards that are fully confirmed (not pending partial/override)."""
        pending_lines = {
            str(r.get("line_id"))
            for r in (self.partial_requests or [])
            if r.get("status") == "pending" and r.get("line_id")
        }
        return {
            str(lid): str(vid)
            for lid, vid in (self.awards or {}).items()
            if lid and vid and str(lid) not in pending_lines
        }

    def _manager_email(self) -> str:
        return "manager@aerchain.example"

    def _write_manager_outbox(
        self,
        *,
        notice_kind: str,
        subject: str,
        body: str,
        request_id: str = "",
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Stub a manager notification into data/outbox + dispatch_log (kind=manager)."""
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        rfx_id = (self.rfx.rfx_id if self.rfx else "UNKNOWN")
        slug = request_id or notice_kind
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(slug))[:48] or "notice"
        fname = f"manager_{notice_kind}_{safe}_{re.sub(r'[^A-Za-z0-9._-]+', '_', rfx_id)}.txt"
        path = OUTBOX_DIR / fname
        sent_at = self._now_iso()
        artifact = (
            f"To: {self._manager_email()}\n"
            f"Subject: {subject}\n"
            f"From: buyer@aerchain.example\n"
            f"Date: {sent_at}\n"
            f"\n"
            f"{body}"
        )
        path.write_text(artifact, encoding="utf-8")
        preview = " ".join(body.split())
        if len(preview) > 220:
            preview = preview[:217] + "…"
        record: dict[str, Any] = {
            "kind": "manager",
            "notice_kind": notice_kind,
            "vendor_id": "manager",
            "vendor_name": "Manager",
            "to": self._manager_email(),
            "subject": subject,
            "body": body,
            "body_preview": preview,
            "rfx_id": rfx_id,
            "sent_at": sent_at,
            "status": "stub_sent",
            "delivery": "stub_sent",
            "path": str(path),
            "request_id": request_id or "",
        }
        if extra:
            record.update(extra)
        # Keep prior manager + RFQ + award rows; append this notice
        self.dispatch_log = list(self.dispatch_log or [])
        self.dispatch_log.append(record)
        self.dispatch_paths = list(self.dispatch_paths or []) + [str(path)]
        return record

    def _notify_manager_approval_needed(self, entry: dict[str, Any]) -> dict[str, Any]:
        kind = str(entry.get("kind") or "partial")
        gaps = entry.get("gaps") or []
        gap_txt = "; ".join(str(g) for g in gaps[:6]) if gaps else "(none)"
        suggested = entry.get("suggested_vendor_id") or ""
        subject = (
            f"[Manager approval needed] {entry.get('line_id')} → "
            f"{entry.get('vendor_name') or entry.get('vendor_id')} ({kind})"
        )
        body = (
            f"A buyer requested manager approval on RFx "
            f"{self.rfx.rfx_id if self.rfx else ''}.\n\n"
            f"Request ID: {entry.get('request_id')}\n"
            f"Kind: {kind}\n"
            f"Line: {entry.get('line_id')}\n"
            f"Vendor: {entry.get('vendor_name') or entry.get('vendor_id')} "
            f"({entry.get('vendor_id')})\n"
            f"Unit price (INR): {entry.get('unit_price_inr')}\n"
            f"Suggested vendor (system): {suggested or '—'}\n"
            f"Gaps / reason:\n  {gap_txt}\n\n"
            f"Buyer note:\n  {entry.get('buyer_note') or '(none)'}\n\n"
            f"This is a stub email (no SMTP). Approve/reject via Audit/API if needed.\n"
        )
        return self._write_manager_outbox(
            notice_kind="approval",
            subject=subject,
            body=body,
            request_id=str(entry.get("request_id") or ""),
            extra={
                "line_id": entry.get("line_id"),
                "related_vendor_id": entry.get("vendor_id"),
            },
        )

    def _notify_manager_awards_sent(self, vendor_names: list[str]) -> dict[str, Any]:
        names = vendor_names or []
        if len(names) == 1:
            who = f"vendor {names[0]}"
        elif not names:
            who = "no vendors (nothing firm to send)"
        else:
            who = "vendors " + ", ".join(names)
        subject = f"[Awards sent] Notices stub-sent for {who}"
        body = (
            f"Award notices were stub-sent on RFx "
            f"{self.rfx.rfx_id if self.rfx else ''}.\n\n"
            f"Confirmed vendors notified:\n"
            + ("\n".join(f"  - {n}" for n in names) if names else "  (none)")
            + "\n\nPending partial/override vendors were NOT emailed.\n"
            "This is a stub email (no SMTP).\n"
        )
        return self._write_manager_outbox(
            notice_kind="awards_sent",
            subject=subject,
            body=body,
            request_id=f"AS-{uuid.uuid4().hex[:8].upper()}",
            extra={"vendors": names},
        )

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
            "kind": "partial",
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
            "suggested_vendor_id": str((self.suggested_awards or {}).get(line_id) or ""),
        }
        self.partial_requests.append(entry)
        gap_txt = "; ".join(entry["gaps"][:3]) if entry["gaps"] else "partial qualification"
        self.append_review_log(
            "partial_award_requested",
            f"{req_id} · {line_id} → {vendor_id} · {gap_txt} · note: {note[:120]}",
            actor="buyer",
            persist=False,
        )
        try:
            self._notify_manager_approval_needed(entry)
        except Exception:
            pass
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
        # Freeze UI removed — never lock awards (legacy freeze snapshots ignored)
        return False

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
        # Ensure we have a system baseline to detect overrides against.
        if not self.suggested_awards:
            try:
                baseline = suggest_split_award(
                    self.comparison,
                    qualifications=self.qualified_vendor_ids(),
                    rfx=self.rfx,
                )
                base_map: dict[str, str] = {}
                for row in baseline.get("awards") or []:
                    lid = str(row.get("line_id") or "")
                    vid = str(row.get("vendor_id") or "")
                    if lid and vid:
                        base_map[lid] = vid
                self.suggested_awards = base_map
            except Exception:
                self.suggested_awards = dict(self.awards or {})

        approved_map = {
            str(r.get("line_id")): str(r.get("vendor_id"))
            for r in self.partial_requests
            if r.get("status") == "approved" and r.get("line_id") and r.get("vendor_id")
        }
        qual = set(self.qualified_vendor_ids())
        names = dict(self.comparison.vendor_names or {})
        names = {**{v.vendor_id: v.name for v in self.rfx.vendors}, **names}
        price_map = {
            (c.line_id, c.vendor_id): c.unit_price_inr for c in self.comparison.cells
        }

        firm_input: dict[str, str] = {}
        partial_keep: dict[str, str] = {}
        override_created: list[dict[str, Any]] = []

        submitted = {str(k): str(v) for k, v in (awards or {}).items() if k and v}

        for lid, vid in submitted.items():
            suggested = str((self.suggested_awards or {}).get(lid) or "")
            is_override = bool(suggested and vid != suggested)

            if vid not in qual:
                # Non-Pass: only keep if already manager-approved partial
                if approved_map.get(lid) == vid:
                    partial_keep[lid] = vid
                else:
                    # Do not silently firm a Fail vendor — buyer should use partial request
                    continue
            elif is_override:
                # Pass→Pass (or any Pass) change from auto-suggest → provisional override
                # Drop superseded pending for this line
                kept: list[dict[str, Any]] = []
                for req in self.partial_requests:
                    if req.get("line_id") == lid and req.get("status") == "pending":
                        continue
                    kept.append(req)
                self.partial_requests = kept
                req_id = f"OV-{uuid.uuid4().hex[:8].upper()}"
                entry = {
                    "request_id": req_id,
                    "kind": "override",
                    "line_id": lid,
                    "vendor_id": vid,
                    "vendor_name": names.get(vid, vid),
                    "unit_price_inr": price_map.get((lid, vid)),
                    "gaps": [
                        f"Buyer override from suggested {suggested} → {vid}",
                    ],
                    "buyer_note": f"Override auto-suggested award ({suggested} → {vid})",
                    "status": "pending",
                    "manager_comment": "",
                    "requested_at": self._now_iso(),
                    "resolved_at": "",
                    "provisional": True,
                    "suggested_vendor_id": suggested,
                }
                self.partial_requests.append(entry)
                override_created.append(entry)
                self.append_review_log(
                    "award_override_requested",
                    f"{req_id} · {lid} · {suggested} → {vid}",
                    actor="buyer",
                    persist=False,
                )
                try:
                    self._notify_manager_approval_needed(entry)
                except Exception:
                    pass
                # Line stays provisional — not in firm awards
            else:
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
        for lid, vid in partial_keep.items():
            cleaned[lid] = vid

        # Do not carry firm awards for lines with a pending override/partial
        pending_lines = {
            str(r.get("line_id"))
            for r in self.partial_requests
            if r.get("status") == "pending"
        }
        for lid in list(cleaned.keys()):
            if lid in pending_lines:
                cleaned.pop(lid, None)

        self.awards = cleaned
        self.award_validation = result
        self.step = "awarded"
        self.wizard_step = "award"
        n = len(cleaned)
        total = (result.get("totals") or {}).get("grand_total_inr") or (
            result.get("totals") or {}
        ).get("grand_total_partial_inr") or 0.0
        if self.rfx:
            for lid, vid in partial_keep.items():
                for req in self.partial_requests:
                    if (
                        req.get("line_id") == lid
                        and req.get("vendor_id") == vid
                        and req.get("status") == "approved"
                    ):
                        qty = 1.0
                        for li in self.rfx.line_items:
                            if li.line_id == lid:
                                qty = float(li.qty or 1)
                                break
                        total = float(total) + float(req.get("unit_price_inr") or 0) * qty
                        break
        detail = f"{n} line(s) · ₹{float(total):.2f}"
        if override_created:
            detail += f" · {len(override_created)} override(s) pending manager"
        self.append_review_log(
            "award_saved",
            detail,
            actor="buyer",
            persist=False,
        )
        self._persist()
        result = dict(result or {})
        result["_overrides_pending"] = override_created
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
        self.suggested_awards = dict(cleaned)
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


    def recommended_split_blurb(self) -> dict[str, Any]:
        """Plain-English payload for the Award 'Recommended split' banner (no fluff)."""
        total_lines = len(self.rfx.line_items) if self.rfx else 0
        pass_ids = set(self.qualified_vendor_ids())
        empty = {
            "sentence": "Cheapest per line among Pass vendors.",
            "total_inr": 0.0,
            "lines_awarded": 0,
            "lines_total": total_lines,
            "by_vendor": [],
            "has_pass": bool(pass_ids),
            "has_awards": bool(self.awards),
        }
        if not self.comparison or not self.rfx:
            return empty
        if not pass_ids:
            empty["sentence"] = "No Pass vendors — nothing to recommend until knockouts clear."
            return empty

        summary = self.award_summary() if (self.awards or self.award_validation) else None
        # Prefer live awards; fall back to suggested_awards / cheapest split preview
        awards_map = dict(self.awards or {}) or dict(self.suggested_awards or {})
        if not awards_map and not summary:
            try:
                preview = suggest_split_award(
                    self.comparison,
                    qualifications=self.qualified_vendor_ids(),
                    rfx=self.rfx,
                )
                cleaned: dict[str, str] = {}
                for row in preview.get("awards") or []:
                    lid = str(row.get("line_id") or "")
                    vid = str(row.get("vendor_id") or "")
                    if lid and vid:
                        cleaned[lid] = vid
                awards_map = cleaned
                totals = preview.get("totals") or {}
                by_vendor_raw = totals.get("by_vendor") or {}
                by_vendor = []
                for vid, bucket in by_vendor_raw.items():
                    if isinstance(bucket, dict):
                        by_vendor.append(
                            {
                                "vendor_id": str(vid),
                                "name": bucket.get("vendor_name")
                                or bucket.get("name")
                                or str(vid),
                                "lines": int(bucket.get("lines") or 0),
                                "extended_inr": float(
                                    bucket.get("extended_inr")
                                    or bucket.get("extended_partial_inr")
                                    or 0
                                ),
                            }
                        )
                    else:
                        by_vendor.append(
                            {
                                "vendor_id": str(vid),
                                "name": str(vid),
                                "lines": 0,
                                "extended_inr": float(bucket or 0),
                            }
                        )
                by_vendor.sort(key=lambda b: (-b["lines"], b["name"]))
                return {
                    "sentence": "Cheapest per line among Pass vendors.",
                    "total_inr": float(
                        totals.get("grand_total_inr")
                        or totals.get("grand_total_partial_inr")
                        or 0
                    ),
                    "lines_awarded": int(totals.get("lines_awarded") or len(awards_map)),
                    "lines_total": total_lines,
                    "by_vendor": by_vendor,
                    "has_pass": True,
                    "has_awards": False,
                }
            except Exception:
                return empty

        if summary:
            by_vendor_raw = summary.get("by_vendor") or {}
            total_inr = float(summary.get("total_inr") or 0)
            lines_awarded = len(summary.get("lines") or awards_map)
        else:
            by_vendor_raw = {}
            total_inr = 0.0
            lines_awarded = len(awards_map)

        # Rebuild by_vendor with line counts when summary fallback lacks them
        names = {v.vendor_id: v.name for v in self.rfx.vendors}
        if self.comparison:
            names.update(self.comparison.vendor_names or {})
        counts: dict[str, int] = {}
        for lid, vid in awards_map.items():
            if vid:
                counts[str(vid)] = counts.get(str(vid), 0) + 1

        by_vendor: list[dict[str, Any]] = []
        if by_vendor_raw:
            for vid, bucket in by_vendor_raw.items():
                if isinstance(bucket, dict):
                    by_vendor.append(
                        {
                            "vendor_id": str(vid),
                            "name": bucket.get("name")
                            or bucket.get("vendor_name")
                            or names.get(str(vid), str(vid)),
                            "lines": int(bucket.get("lines") or counts.get(str(vid), 0)),
                            "extended_inr": float(
                                bucket.get("extended_inr")
                                or bucket.get("extended_partial_inr")
                                or 0
                            ),
                        }
                    )
                else:
                    by_vendor.append(
                        {
                            "vendor_id": str(vid),
                            "name": names.get(str(vid), str(vid)),
                            "lines": int(counts.get(str(vid), 0)),
                            "extended_inr": float(bucket or 0),
                        }
                    )
        else:
            # Compute extended from cells
            price_map = {
                (c.line_id, c.vendor_id): c.unit_price_inr for c in self.comparison.cells
            }
            spend: dict[str, float] = {}
            for line in self.rfx.line_items:
                vid = awards_map.get(line.line_id, "")
                if not vid:
                    continue
                unit = price_map.get((line.line_id, vid))
                if unit is None:
                    continue
                spend[vid] = spend.get(vid, 0.0) + float(unit) * float(line.qty or 0)
            total_inr = sum(spend.values())
            for vid, n in counts.items():
                by_vendor.append(
                    {
                        "vendor_id": vid,
                        "name": names.get(vid, vid),
                        "lines": n,
                        "extended_inr": float(spend.get(vid, 0.0)),
                    }
                )

        by_vendor.sort(key=lambda b: (-b["lines"], b["name"]))
        lines_awarded = lines_awarded or sum(b["lines"] for b in by_vendor) or len(awards_map)
        return {
            "sentence": "Cheapest per line among Pass vendors.",
            "total_inr": float(total_inr or 0),
            "lines_awarded": int(lines_awarded),
            "lines_total": total_lines,
            "by_vendor": by_vendor,
            "has_pass": True,
            "has_awards": bool(self.awards),
        }

    def exception_lines(self) -> list[dict[str, Any]]:
        """Lines for the Exceptions card: priced non-Pass candidates and/or pending requests."""
        if not self.rfx or not self.comparison:
            return []
        out: list[dict[str, Any]] = []
        for li in self.rfx.line_items:
            lid = li.line_id
            try:
                partials = self.partial_candidates_for_line(lid)
            except Exception:
                partials = []
            pst = self.partial_status_for_line(lid)
            pending = bool(pst and pst.get("status") == "pending")
            if not partials and not pending and not (
                pst and pst.get("status") in ("approved", "rejected")
            ):
                # Only surface if there is something actionable or a live request status
                continue
            if not partials and not pst:
                continue
            # Skip lines with no partial candidates and no pending/recent request
            if not partials and not pst:
                continue
            out.append(
                {
                    "line_id": lid,
                    "description": li.description,
                    "qty": li.qty,
                    "partials": partials,
                    "status": pst,
                    "pending": pending,
                }
            )
        # Prefer lines that still need action (have partials or pending)
        actionable = [
            row
            for row in out
            if row.get("pending") or (row.get("partials") and not (
                row.get("status") and row["status"].get("status") == "approved"
            ))
        ]
        return actionable if actionable else [
            row for row in out if row.get("partials") or row.get("pending")
        ]

    def explain_award(self) -> str:
        """Ask Analyst for a 4–6 sentence defensible award brief; persist on snapshot."""
        if not self.comparison:
            text = (
                "No comparison matrix yet — open Compare and build it before I can "
                "explain an award split."
            )
            self.award_explanation = text
            self._persist()
            return text
        awards_map = dict(self.awards or {}) or dict(self.suggested_awards or {})
        blurb = self.recommended_split_blurb()
        result = analyst_explain_award(
            self.rfx,
            self.comparison,
            awards=awards_map,
            summary=blurb,
            history=self.analyst_history,
        )
        text = (
            (result.get("answer") if isinstance(result, dict) else None)
            or (result.get("markdown") if isinstance(result, dict) else None)
            or (str(result) if result else "")
        ).strip()
        if not text:
            text = (
                "I couldn't draft an explanation just now. "
                "Try Suggest cheapest split first, then Explain again."
            )
        self.award_explanation = text
        # Keep a light trail without polluting Ask chat heavily
        self.append_review_log(
            "award_explanation",
            f"explain · {len(text)} chars",
            persist=False,
        )
        self._persist()
        return text

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
        """Assemble evidence drawer payload for one comparison cell.

        Enrichment includes the underlying vendor artifact (path/url/kind plus a
        short text preview for email/txt) so Compare can show PDF/image/email
        media — not just snippets.
        """
        # Cold-start: binaries live under /tmp and vanish; re-seed before resolve.
        try:
            self._ensure_inbox_files()
        except Exception:
            logging.getLogger(__name__).debug(
                "evidence_for_cell: _ensure_inbox_files failed", exc_info=True
            )

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
                loc = str(item.get("location") or "")
                snippets.append({"snippet": sn, "location": loc})
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
                msg_vid = str(
                    getattr(msg, "parsed_vendor_id", "")
                    or getattr(msg, "vendor_id", "")
                    or ""
                )
                if msg_vid == vendor_id:
                    source_file = str(getattr(msg, "path", "") or "")
                    if source_file:
                        break
        if not source_file and quote:
            # Last resort: source_format is often just "pdf"/"png" — keep for badge.
            source_file = str(getattr(quote, "source_format", "") or "")

        resolved = self.resolve_source_path(vendor_id, hint=source_file)
        source_path = str(resolved) if resolved else ""
        source_name = (
            Path(source_path).name
            if source_path
            else (Path(source_file).name if source_file else "")
        )
        # Prefer extension of the real file; fall back to quote.source_format.
        source_kind = _source_kind_from_name(source_name or source_file)
        if source_kind == "unknown" and quote and getattr(quote, "source_format", None):
            fmt = str(quote.source_format or "").lower().strip()
            if fmt in {
                "pdf",
                "image",
                "email",
                "text",
                "xlsx",
                "docx",
                "json",
                "csv",
                "png",
                "txt",
            }:
                source_kind = {
                    "png": "image",
                    "txt": "text",
                }.get(fmt, fmt)

        source_preview_text = ""
        if resolved and source_kind in {"email", "text", "json", "csv", "unknown"}:
            source_preview_text = _read_preview_text(resolved, limit=4096)
            if source_kind == "text" and _looks_like_email(source_preview_text):
                source_kind = "email"
            # Cap preview for the drawer (~2–4KB already); trim for template safety.
            if len(source_preview_text) > 4000:
                source_preview_text = source_preview_text[:4000] + "\n…"

        # Display label: prefer real filename over bare format token.
        display_source = source_name or source_file

        rfx_id = self.rfx.rfx_id if self.rfx else ""
        source_url = ""
        if rfx_id and vendor_id and (resolved or source_name):
            from urllib.parse import quote as _urlquote

            source_url = (
                f"/crew/{_urlquote(rfx_id)}/source-file"
                f"?vendor_id={_urlquote(vendor_id)}"
            )

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
            "source_file": display_source,
            "source_path": source_path,
            "source_url": source_url,
            "source_kind": source_kind,
            "source_preview_text": source_preview_text,
            "snippets": snippets,
            "notes": getattr(quote, "notes", "") if quote else "",
            "confidence": getattr(quote, "confidence", None) if quote else None,
        }

    def resolve_source_path(
        self, vendor_id: str, *, hint: str = ""
    ) -> Optional[Path]:
        """Locate the vendor binary under this RFX inbox (preferred) or VENDOR_DIR.

        Path-traversal safe: only returns files that resolve under the RFX inbox
        directory or the seeded vendor_responses fixtures.
        """
        try:
            self._ensure_inbox_files()
        except Exception:
            pass

        inbox_root: Optional[Path] = None
        if self.rfx:
            try:
                inbox_root = self._inbox_dir()
            except Exception:
                inbox_root = None

        allowed_roots: list[Path] = []
        if inbox_root and inbox_root.exists():
            allowed_roots.append(inbox_root.resolve())
        if VENDOR_DIR.exists():
            allowed_roots.append(VENDOR_DIR.resolve())

        def _safe(path: Path) -> Optional[Path]:
            try:
                if not path.is_file():
                    return None
                resolved = path.resolve()
                for root in allowed_roots:
                    try:
                        resolved.relative_to(root)
                        return resolved
                    except ValueError:
                        continue
            except OSError:
                return None
            return None

        candidates: list[Path] = []

        # 1) Explicit hint (absolute path or basename from quote meta).
        hint_s = str(hint or "").strip()
        if hint_s and hint_s.lower() not in {
            "pdf",
            "png",
            "xlsx",
            "docx",
            "txt",
            "csv",
            "json",
            "email",
            "image",
            "text",
            "unknown",
        }:
            hp = Path(hint_s)
            candidates.append(hp)
            if inbox_root:
                candidates.append(inbox_root / hp.name)
            candidates.append(VENDOR_DIR / hp.name)
            # Strip inbound_ prefix → original fixture name
            name = hp.name
            if name.startswith("inbound_"):
                bare = name[len("inbound_") :]
                candidates.append(VENDOR_DIR / bare)
                if inbox_root:
                    candidates.append(inbox_root / bare)

        # 2) Inbox message path for this vendor.
        for msg in self.inbox or []:
            msg_vid = str(
                getattr(msg, "parsed_vendor_id", "")
                or getattr(msg, "vendor_id", "")
                or ""
            )
            if msg_vid != vendor_id:
                continue
            mp = str(getattr(msg, "path", "") or "")
            if mp:
                candidates.append(Path(mp))
                if inbox_root:
                    candidates.append(inbox_root / Path(mp).name)

        # 3) Filename contains vendor_id (V001 / V01 soft match).
        vid = (vendor_id or "").upper()
        soft = re.sub(r"^V0+", "V", vid) if vid else ""
        search_dirs: list[Path] = []
        if inbox_root and inbox_root.exists():
            search_dirs.append(inbox_root)
        if VENDOR_DIR.exists():
            search_dirs.append(VENDOR_DIR)
        for d in search_dirs:
            try:
                for p in d.iterdir():
                    if not p.is_file() or p.name.startswith("."):
                        continue
                    if p.name.endswith(".extract.json"):
                        continue
                    fname = p.name.upper()
                    if vid and vid in fname:
                        candidates.append(p)
                    elif soft and soft in fname:
                        candidates.append(p)
            except OSError:
                continue

        seen: set[str] = set()
        for c in candidates:
            key = str(c)
            if key in seen:
                continue
            seen.add(key)
            ok = _safe(c)
            if ok:
                return ok
        return None

    def notify_awarded_vendors(self) -> list[str]:
        """Stub-write award_notice_*.txt for firm awards only; manager summary notice."""
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        firm = self.firm_awards_map()
        if not firm:
            raise RuntimeError(
                "No fully confirmed (firm) awards to notify. "
                "Pending partial/override lines are excluded until approved."
            )
        lines = None
        if self.award_validation:
            # Filter validation rows to firm lines only
            raw = list(self.award_validation.get("awards") or [])
            lines = [row for row in raw if str(row.get("line_id") or "") in firm]
        agent = VendorDispatcherAgent(stub=True, outbox_dir=OUTBOX_DIR)
        paths = agent.write_award_notices(self.rfx, firm, lines=lines)
        records = [dict(r) for r in (agent.dispatch_log or []) if r.get("kind") == "award_notice"]
        for r in records:
            r["status"] = "stub_sent"
            r["delivery"] = "stub_sent"
            r.setdefault("file", Path(str(r.get("path") or "")).name)
        self.award_notice_paths = [str(p) for p in paths]
        self.award_log = records
        # Replace prior award_notice rows on Outbox log; keep RFQ + manager notices.
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
        vendor_names = [
            str(r.get("vendor_name") or r.get("vendor_id") or "")
            for r in records
        ]
        vendor_names = [n for n in vendor_names if n]
        try:
            mgr = self._notify_manager_awards_sent(vendor_names)
            # Persist so Award UI can re-render manager card after reload
            self.manager_award_notice = dict(mgr) if mgr else None
        except Exception:
            self.manager_award_notice = None
        names = ", ".join(Path(p).name for p in self.award_notice_paths) or "(none)"
        self.append_review_log(
            "award_notices_sent",
            f"{len(self.award_notice_paths)} notice(s) stub_sent (firm only): {names}",
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
            "manager_award_notice": dict(self.manager_award_notice) if self.manager_award_notice else None,
            "freeze": self.freeze,
            "review_log": list(self.review_log or []),
            "partial_requests": list(self.partial_requests or []),
            "suggested_awards": dict(self.suggested_awards or {}),
            "award_explanation": self.award_explanation,
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
        man = data.get("manager_award_notice")
        self.manager_award_notice = dict(man) if isinstance(man, dict) else None
        self.freeze = data.get("freeze") or None
        self.review_log = list(data.get("review_log") or [])
        self.partial_requests = list(data.get("partial_requests") or [])
        self.suggested_awards = {
            str(k): str(v) for k, v in dict(data.get("suggested_awards") or {}).items() if k and v
        }
        expl = data.get("award_explanation")
        self.award_explanation = str(expl).strip() if expl else None

    def _persist(self) -> None:
        """Write snapshot to local STORE_DIR (best effort) and Blob when configured.

        On Vercel, /tmp is instance-local. When BLOB_READ_WRITE_TOKEN is set the
        pipeline JSON (quotes, comparison, awards, inbox metadata, dispatch_log)
        is also written via core.storage.save_state so cold starts can reload.

        When running on Vercel with Blob configured, a Blob failure with no local
        write is re-raised. If /tmp succeeded, we log loudly and continue so the
        browser localStorage rehydrate path can cover the next cold start.
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

        blob_wanted = False
        blob_ok = False
        blob_exc: Optional[BaseException] = None
        try:
            from core import storage as _storage

            blob_wanted = bool(
                _storage.blob_configured()
                or os.environ.get("BLOB_READ_WRITE_TOKEN")
                or _storage.backend_name() == "vercel-blob"
            )
            if blob_wanted:
                # Copy so storage.save_state can attach _version without mutating snap.
                _storage.save_state(self.rfx.rfx_id, dict(snap))
                blob_ok = True
        except Exception as exc:  # noqa: BLE001
            blob_exc = exc
            logging.getLogger(__name__).warning(
                "blob save_state(%s) failed: %s", self.rfx.rfx_id, exc
            )

        on_vercel = bool(
            os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        )
        if blob_wanted and not blob_ok and on_vercel and blob_exc is not None:
            # Durable Blob failed. If we have no local copy either, fail hard.
            # If /tmp wrote successfully, keep going so the browser can cache
            # session.json and POST /crew/rehydrate after the next cold start
            # (Blob may be suspended/flaky; demos must still survive).
            if not local_ok:
                raise RuntimeError(
                    f"Blob persist failed for {self.rfx.rfx_id} on Vercel "
                    f"and local store also failed: {blob_exc}"
                ) from blob_exc
            logging.getLogger(__name__).error(
                "Blob persist failed for %s on Vercel (%s); "
                "instance-local /tmp only — cold start needs browser rehydrate",
                self.rfx.rfx_id,
                blob_exc,
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

"""
Agent 2 — Vendor Dispatcher (Send stage).

Packages an RFx into clear per-vendor cover emails. Default stub=True writes
plaintext messages under data/outbox and a JSON dispatch log with status
stubbed|sent|error:* — no SMTP leave-the-box, no LLM.
"""

from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional, Union

from shared_models import LineItem, QuestionnaireItem, RFx, Vendor

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTBOX = _REPO_ROOT / "data" / "outbox"

RFxLike = Union[RFx, dict[str, Any]]
VendorLike = Union[Vendor, dict[str, Any]]


def _as_dict(rfx: RFxLike) -> dict[str, Any]:
    if isinstance(rfx, RFx):
        return rfx.model_dump()
    if isinstance(rfx, dict):
        return dict(rfx)
    if hasattr(rfx, "model_dump"):
        return rfx.model_dump()
    raise TypeError(f"expected RFx or dict, got {type(rfx)!r}")


def _vendor_fields(vendor: VendorLike) -> dict[str, str]:
    if isinstance(vendor, Vendor):
        return {
            "vendor_id": vendor.vendor_id,
            "name": vendor.name,
            "email": vendor.email,
        }
    if not isinstance(vendor, dict):
        vendor = dict(vendor) if hasattr(vendor, "items") else {}
    return {
        "vendor_id": str(vendor.get("vendor_id") or vendor.get("id") or ""),
        "name": str(vendor.get("name") or "Vendor"),
        "email": str(vendor.get("email") or vendor.get("contact") or ""),
    }


def _line_fields(item: Any) -> dict[str, Any]:
    if isinstance(item, LineItem):
        return {
            "line_id": item.line_id,
            "description": item.description,
            "qty": item.qty,
            "uom": item.uom,
        }
    if not isinstance(item, dict):
        item = item.model_dump() if hasattr(item, "model_dump") else {}
    return {
        "line_id": str(item.get("line_id") or item.get("id") or ""),
        "description": str(item.get("description") or ""),
        "qty": item.get("qty", 0),
        "uom": str(item.get("uom") or "piece"),
    }


def _question_text(q: Any, index: int) -> str:
    if isinstance(q, QuestionnaireItem):
        flag = " [knockout]" if getattr(q, "knockout", False) else ""
        return f"{q.question}{flag}"
    if isinstance(q, str):
        return q
    if isinstance(q, dict):
        text = str(q.get("question") or q.get("text") or q)
        if q.get("knockout"):
            return f"{text} [knockout]"
        return text
    if hasattr(q, "question"):
        return str(q.question)
    return str(q)


def _fmt_qty(qty: Any) -> str:
    try:
        n = float(qty)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.4g}"
    except (TypeError, ValueError):
        return str(qty)


def _safe_slug(value: str) -> str:
    keep = []
    for ch in str(value):
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep) or "unknown"


def _build_line_table(line_items: list[Any]) -> str:
    rows = [_line_fields(li) for li in line_items]
    if not rows:
        return "(no line items)"
    id_w = max(7, max(len(str(r["line_id"])) for r in rows))
    desc_w = max(11, min(48, max(len(r["description"]) for r in rows)))
    qty_w = max(3, max(len(_fmt_qty(r["qty"])) for r in rows))
    uom_w = max(3, max(len(r["uom"]) for r in rows))
    header = (
        f"{'line_id':<{id_w}}  {'description':<{desc_w}}  "
        f"{'qty':>{qty_w}}  {'uom':<{uom_w}}"
    )
    rule = "-" * len(header)
    body_lines = []
    for r in rows:
        desc = r["description"]
        if len(desc) > desc_w:
            desc = desc[: desc_w - 1] + "…"
        body_lines.append(
            f"{str(r['line_id']):<{id_w}}  {desc:<{desc_w}}  "
            f"{_fmt_qty(r['qty']):>{qty_w}}  {r['uom']:<{uom_w}}"
        )
    return "\n".join([header, rule, *body_lines])


def build_cover_email(rfx: RFxLike, vendor: VendorLike) -> dict[str, str]:
    """
    Build a single cover email (To / Subject / Body) for UI preview or send.

    Does not write to disk. Accepts shared_models.RFx/Vendor or plain dicts.
    """
    data = _as_dict(rfx)
    v = _vendor_fields(vendor)
    lines_block = _build_line_table(data.get("line_items") or [])
    questions = data.get("questionnaire") or []
    if questions:
        q_block = "\n".join(
            f"  Q{i + 1}. {_question_text(q, i)}" for i, q in enumerate(questions)
        )
    else:
        q_block = "  (none)"

    optional: list[str] = []
    deadline = data.get("response_deadline") or data.get("deadline")
    if deadline:
        optional.append(f"Response deadline : {deadline}")
    delivery = data.get("delivery_location") or data.get("delivery")
    if delivery:
        optional.append(f"Delivery location : {delivery}")
    optional_block = ("\n".join(optional) + "\n") if optional else ""

    scope = (data.get("scope") or "").strip()
    scope_block = f"Scope\n{scope}\n\n" if scope else ""

    n_lines = len(data.get("line_items") or [])
    n_qs = len(questions)
    subject = (
        f"RFx Invitation — {data.get('rfx_id', '')}: {data.get('title', '')}"
    ).strip()

    body = f"""Dear {v['name']},

You are invited to submit a quotation for the RFx below.

RFx ID            : {data.get('rfx_id', '')}
Title             : {data.get('title', '')}
Payment / terms   : {data.get('terms', '')}
Currency          : {data.get('currency', 'INR')}
Line items        : {n_lines}
Questionnaire     : {n_qs} question(s)
{optional_block}
{scope_block}── LINE ITEMS ──────────────────────────────────────────────────────────────
{lines_block}

── QUALITY QUESTIONNAIRE ───────────────────────────────────────────────────
Please answer the following for qualification:
{q_block}

── SUBMISSION INSTRUCTIONS ─────────────────────────────────────────────────
Reply in any convenient format (Excel, PDF, Word, or plain email).
Include unit prices, UOM, and quote currency. Prices exclusive of tax unless
noted. Reference RFx {data.get('rfx_id', '')} in your reply.

Regards,
Procurement Team
"""

    return {
        "vendor_id": v["vendor_id"],
        "vendor_name": v["name"],
        "to": v["email"],
        "subject": subject,
        "body": body,
        "from": "procurement@company.com",
    }


def preview_cover_emails(rfx: RFxLike) -> list[dict[str, str]]:
    """
    UI helper: preview every vendor cover email before Send.

    Returns a list of dicts with keys vendor_id, vendor_name, to, subject, body.
    Does not write outbox files or touch SMTP.
    """
    data = _as_dict(rfx)
    return [build_cover_email(data, v) for v in (data.get("vendors") or [])]


# Alias expected by product UX copy
preview_cover_email = build_cover_email


class VendorDispatcherAgent:
    """
    Send-stage agent: one cover email per vendor → data/outbox/.

    stub=True (default): status "stubbed", SMTP logged only (no network send).
    stub=False: attempt SMTP; status "sent" or "error: …".
    """

    def __init__(
        self,
        stub: bool = True,
        outbox_dir: str | os.PathLike | None = None,
        smtp_host: str = "localhost",
        smtp_port: int = 1025,
        from_addr: str = "procurement@company.com",
    ) -> None:
        self.stub = stub
        self.outbox_dir = Path(outbox_dir) if outbox_dir else _DEFAULT_OUTBOX
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.from_addr = from_addr
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.dispatch_log: list[dict[str, Any]] = []

    def preview(self, rfx: RFxLike) -> list[dict[str, str]]:
        """Instance wrapper around preview_cover_emails for the UI."""
        return preview_cover_emails(rfx)

    def dispatch(self, rfx: RFxLike) -> list[str]:
        """
        Write one plaintext cover email per vendor under outbox_dir.

        Returns outbox email file paths. Also writes dispatch_log_<rfx_id>.json
        with status stubbed|sent|error:* and populates self.dispatch_log.
        """
        data = _as_dict(rfx)
        rfx_id = str(data.get("rfx_id") or "UNKNOWN")
        vendors = data.get("vendors") or []
        paths: list[str] = []
        records: list[dict[str, Any]] = []
        sent_at = datetime.now(timezone.utc).astimezone().isoformat()
        self.dispatch_log = []

        for vendor_raw in vendors:
            cover = build_cover_email(data, vendor_raw)
            cover["from"] = self.from_addr
            to_addr = cover["to"]
            subject = cover["subject"]
            body = cover["body"]
            filename = (
                f"invite_{_safe_slug(cover['vendor_id'])}_{_safe_slug(rfx_id)}.txt"
            )
            email_path = self.outbox_dir / filename

            record: dict[str, Any] = {
                "vendor_id": cover["vendor_id"],
                "vendor_name": cover["vendor_name"],
                "to": to_addr,
                "subject": subject,
                "rfx_id": rfx_id,
                "sent_at": sent_at,
                "status": None,
                "delivery": None,
                "path": str(email_path),
            }

            artifact = (
                f"To: {to_addr}\n"
                f"Subject: {subject}\n"
                f"From: {self.from_addr}\n"
                f"Date: {sent_at}\n"
                f"\n"
                f"{body}"
            )
            email_path.write_text(artifact, encoding="utf-8")
            paths.append(str(email_path))

            if self.stub:
                record["status"] = "stubbed"
                record["delivery"] = "stubbed"
            else:
                try:
                    msg = MIMEText(body, "plain", "utf-8")
                    msg["From"] = self.from_addr
                    msg["To"] = to_addr
                    msg["Subject"] = subject
                    with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                        server.sendmail(self.from_addr, [to_addr], msg.as_string())
                    record["status"] = "sent"
                    record["delivery"] = "sent"
                except Exception as exc:  # noqa: BLE001 — surface as status
                    record["status"] = f"error: {exc}"
                    record["delivery"] = "error"

            records.append(record)
            self.dispatch_log.append(record)

        log_path = self.outbox_dir / f"dispatch_log_{_safe_slug(rfx_id)}.json"
        log_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return paths


    def write_award_notices(
        self,
        rfx: RFxLike,
        awards: dict[str, str],
        *,
        lines: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """
        Stub award notices for each vendor that won ≥1 line.

        Writes award_notice_<vendor>_<rfx>.txt under outbox_dir.
        awards: {line_id: vendor_id}. No SMTP. Returns outbox paths.
        Also appends stubbed rows to self.dispatch_log and writes
        award_log_<rfx_id>.json.
        """
        data = _as_dict(rfx)
        rfx_id = str(data.get("rfx_id") or "UNKNOWN")
        vendors = {_vendor_fields(v)["vendor_id"]: _vendor_fields(v) for v in (data.get("vendors") or [])}
        grouped = _group_awards(awards, data, lines=lines)
        paths: list[str] = []
        records: list[dict[str, Any]] = []
        sent_at = datetime.now(timezone.utc).astimezone().isoformat()

        for vendor_id, won in grouped.items():
            vendor = vendors.get(vendor_id) or {
                "vendor_id": vendor_id,
                "name": (won[0].get("vendor_name") if won else "") or vendor_id,
                "email": "",
            }
            # Prefer enriched vendor_name from award rows when RFx vendor missing
            if won and won[0].get("vendor_name") and vendor["name"] == vendor_id:
                vendor = {**vendor, "name": str(won[0]["vendor_name"])}
            notice = build_award_notice(data, vendor, won)
            notice["from"] = self.from_addr
            filename = (
                f"award_notice_{_safe_slug(vendor_id)}_{_safe_slug(rfx_id)}.txt"
            )
            email_path = self.outbox_dir / filename
            artifact = (
                f"To: {notice['to']}\n"
                f"Subject: {notice['subject']}\n"
                f"From: {self.from_addr}\n"
                f"Date: {sent_at}\n"
                f"\n"
                f"{notice['body']}"
            )
            email_path.write_text(artifact, encoding="utf-8")
            paths.append(str(email_path))
            record = {
                "kind": "award_notice",
                "vendor_id": vendor_id,
                "vendor_name": notice["vendor_name"],
                "to": notice["to"],
                "subject": notice["subject"],
                "rfx_id": rfx_id,
                "sent_at": sent_at,
                "status": "stubbed",
                "delivery": "stubbed",
                "path": str(email_path),
                "lines_won": [w.get("line_id") for w in won],
            }
            records.append(record)
            self.dispatch_log.append(record)

        log_path = self.outbox_dir / f"award_log_{_safe_slug(rfx_id)}.json"
        log_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return paths



def _line_lookup(rfx_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map line_id -> line fields from the RFx."""
    out: dict[str, dict[str, Any]] = {}
    for item in rfx_data.get("line_items") or []:
        row = _line_fields(item)
        if row["line_id"]:
            out[str(row["line_id"])] = row
    return out


def _group_awards(
    awards: dict[str, str],
    rfx_data: dict[str, Any],
    lines: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    Group awards by vendor_id.

    awards: {line_id: vendor_id}
    lines: optional enriched rows (from award_summary / validate_award).
    """
    by_line: dict[str, dict[str, Any]] = {}
    if lines:
        for row in lines:
            lid = str(row.get("line_id") or "")
            if lid:
                by_line[lid] = dict(row)

    lookup = _line_lookup(rfx_data)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line_id, vendor_id in (awards or {}).items():
        lid = str(line_id)
        vid = str(vendor_id or "")
        if not lid or not vid:
            continue
        base = dict(lookup.get(lid) or {"line_id": lid, "description": "", "qty": "", "uom": "piece"})
        extra = by_line.get(lid) or {}
        row = {
            "line_id": lid,
            "description": str(extra.get("description") or base.get("description") or ""),
            "qty": extra.get("qty", base.get("qty")),
            "uom": str(extra.get("uom") or base.get("uom") or "piece"),
            "unit_price_inr": extra.get("unit_price_inr"),
            "extended_inr": extra.get("extended_inr"),
            "vendor_id": vid,
            "vendor_name": str(extra.get("vendor_name") or ""),
        }
        grouped.setdefault(vid, []).append(row)
    return grouped


def build_award_notice(
    rfx: RFxLike,
    vendor: VendorLike,
    won_lines: list[dict[str, Any]],
) -> dict[str, str]:
    """
    Build a stub award-notice email (To / Subject / Body) for one vendor.

    Does not write to disk. won_lines should list the lines this vendor won
    (line_id, description, qty, optional unit_price_inr / extended_inr).
    """
    data = _as_dict(rfx)
    v = _vendor_fields(vendor)
    rfx_id = str(data.get("rfx_id") or "")
    title = str(data.get("title") or "")

    if not won_lines:
        table = "(no lines awarded)"
    else:
        rows = []
        for w in won_lines:
            lid = str(w.get("line_id") or "")
            desc = str(w.get("description") or "")
            qty = _fmt_qty(w.get("qty", ""))
            uom = str(w.get("uom") or "piece")
            unit = w.get("unit_price_inr")
            ext = w.get("extended_inr")
            price_bit = ""
            if unit is not None:
                try:
                    price_bit = f"  @ INR {float(unit):,.2f}/{uom}"
                except (TypeError, ValueError):
                    price_bit = f"  @ {unit}"
            if ext is not None:
                try:
                    price_bit += f"  (ext INR {float(ext):,.2f})"
                except (TypeError, ValueError):
                    price_bit += f"  (ext {ext})"
            rows.append(f"  {lid:>6}  {desc}  qty {qty} {uom}{price_bit}")
        table = "\n".join(rows)

    subject = f"Award notice — {rfx_id}: {title}".strip(" :")
    body = f"""Dear {v['name']},

Congratulations — you have been selected for the following line(s) on RFx {rfx_id}.

RFx ID   : {rfx_id}
Title    : {title}
Currency : {data.get('currency', 'INR')}

── LINES AWARDED ───────────────────────────────────────────────────────────
{table}

This is a stub award notice for internal review (SMTP not sent). A formal
purchase order / contract will follow under separate cover.

Regards,
Procurement Team
"""
    return {
        "vendor_id": v["vendor_id"],
        "vendor_name": v["name"],
        "to": v["email"],
        "subject": subject,
        "body": body,
        "from": "procurement@company.com",
        "kind": "award_notice",
    }


def write_award_notices(
    rfx: RFxLike,
    awards: dict[str, str],
    *,
    outbox_dir: str | os.PathLike | None = None,
    lines: list[dict[str, Any]] | None = None,
    from_addr: str = "procurement@company.com",
) -> list[str]:
    """
    Stub-write one award_notice_<vendor>_<rfx>.txt per awarded vendor.

    awards: mapping line_id -> vendor_id (pipeline awards map).
    lines: optional enriched award rows (description, prices).
    Returns outbox file paths. No SMTP.
    """
    return VendorDispatcherAgent(
        stub=True, outbox_dir=outbox_dir, from_addr=from_addr
    ).write_award_notices(rfx, awards, lines=lines)


def dispatch_rfx(
    rfx: RFxLike,
    *,
    stub: bool = True,
    outbox_dir: str | os.PathLike | None = None,
) -> list[str]:
    """Module-level helper expected by agents.__init__ / orchestrator."""
    return VendorDispatcherAgent(stub=stub, outbox_dir=outbox_dir).dispatch(rfx)


__all__ = [
    "VendorDispatcherAgent",
    "dispatch_rfx",
    "build_cover_email",
    "preview_cover_email",
    "preview_cover_emails",
    "build_award_notice",
    "write_award_notices",
]

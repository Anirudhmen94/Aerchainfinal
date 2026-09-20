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
]

"""
Agent 2 — Vendor Dispatcher.

Packages an RFx into per-vendor invitation emails. By default (stub=True)
nothing leaves the machine: each message is written as plaintext under
data/outbox and a JSON dispatch log is recorded alongside. No LLM calls.
"""

from __future__ import annotations

import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Union

from shared_models import RFx, Vendor, LineItem, QuestionnaireItem

# Repo root = parent of agents/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTBOX = _REPO_ROOT / "data" / "outbox"

RFxLike = Union[RFx, dict[str, Any]]


def _as_dict(rfx: RFxLike) -> dict[str, Any]:
    """Normalize RFx pydantic model or plain dict into a plain dict."""
    if isinstance(rfx, RFx):
        return rfx.model_dump()
    if isinstance(rfx, dict):
        return dict(rfx)
    # Last resort: pydantic-like objects
    if hasattr(rfx, "model_dump"):
        return rfx.model_dump()
    raise TypeError(f"dispatch expects RFx or dict, got {type(rfx)!r}")


def _vendor_fields(vendor: Any) -> dict[str, str]:
    """Pull vendor_id / name / email, tolerating older id / contact keys."""
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
        "name": str(vendor.get("name") or ""),
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
        return q.question
    if isinstance(q, str):
        return q
    if isinstance(q, dict):
        return str(q.get("question") or q.get("text") or q)
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


def _build_line_table(line_items: list[Any]) -> str:
    rows = [_line_fields(li) for li in line_items]
    if not rows:
        return "(no line items)"
    # Column widths from content
    id_w = max(7, max(len(str(r["line_id"])) for r in rows))
    desc_w = max(11, max(len(r["description"]) for r in rows))
    qty_w = max(3, max(len(_fmt_qty(r["qty"])) for r in rows))
    uom_w = max(3, max(len(r["uom"]) for r in rows))
    header = (
        f"{'line_id':<{id_w}}  {'description':<{desc_w}}  "
        f"{'qty':>{qty_w}}  {'uom':<{uom_w}}"
    )
    rule = "-" * len(header)
    body_lines = [
        f"{str(r['line_id']):<{id_w}}  {r['description']:<{desc_w}}  "
        f"{_fmt_qty(r['qty']):>{qty_w}}  {r['uom']:<{uom_w}}"
        for r in rows
    ]
    return "\n".join([header, rule, *body_lines])


def _build_email_body(rfx: dict[str, Any], vendor: dict[str, str]) -> str:
    lines_block = _build_line_table(rfx.get("line_items") or [])
    questions = rfx.get("questionnaire") or []
    if questions:
        q_block = "\n".join(
            f"  Q{i + 1}. {_question_text(q, i)}" for i, q in enumerate(questions)
        )
    else:
        q_block = "  (none)"

    optional: list[str] = []
    deadline = rfx.get("response_deadline") or rfx.get("deadline")
    if deadline:
        optional.append(f"Response deadline : {deadline}")
    delivery = rfx.get("delivery_location") or rfx.get("delivery")
    if delivery:
        optional.append(f"Delivery location : {delivery}")
    scope = rfx.get("scope")
    if scope:
        optional.append(f"Scope             : {scope}")
    optional_block = ("\n".join(optional) + "\n") if optional else ""

    return f"""Dear {vendor['name']},

You are invited to submit a quotation for the requirements below.

RFx ID            : {rfx.get('rfx_id', '')}
Title             : {rfx.get('title', '')}
Payment / terms   : {rfx.get('terms', '')}
Currency          : {rfx.get('currency', 'INR')}
{optional_block}
── LINE ITEMS ──────────────────────────────────────────────────────────────
{lines_block}

── QUALITY QUESTIONNAIRE ───────────────────────────────────────────────────
Please answer the following for qualification:
{q_block}

── SUBMISSION INSTRUCTIONS ─────────────────────────────────────────────────
Respond in any convenient format (Excel, PDF, Word, or plain email).
State unit prices, UOM, and quote currency. Prices should be exclusive of tax
unless otherwise noted. Reference RFx {rfx.get('rfx_id', '')} in your reply.

Regards,
Procurement Team
"""


def _subject(rfx: dict[str, Any]) -> str:
    return f"RFx Invitation — {rfx.get('rfx_id', '')}: {rfx.get('title', '')}"


def _safe_slug(value: str) -> str:
    keep = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep) or "unknown"


class VendorDispatcherAgent:
    """
    Dispatch RFx invitations to vendors listed on the RFx.

    stub=True (default): write plaintext emails under outbox_dir and a
    JSON dispatch log; do not open an SMTP connection.
    stub=False: attempt real SMTP via smtp_host/smtp_port.
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

    def dispatch(self, rfx: RFxLike) -> list[str]:
        """
        Build and stub/send one invitation per vendor.

        Returns a list of outbox file paths written for this dispatch
        (one plaintext email per vendor). A JSON dispatch log is also
        written under outbox_dir as a side effect.
        """
        data = _as_dict(rfx)
        rfx_id = str(data.get("rfx_id") or "UNKNOWN")
        vendors = data.get("vendors") or []
        paths: list[str] = []
        records: list[dict[str, Any]] = []
        sent_at = datetime.now(timezone.utc).astimezone().isoformat()

        for vendor_raw in vendors:
            vendor = _vendor_fields(vendor_raw)
            body = _build_email_body(data, vendor)
            subject = _subject(data)
            to_addr = vendor["email"]
            filename = (
                f"invite_{_safe_slug(vendor['vendor_id'])}_{_safe_slug(rfx_id)}.txt"
            )
            email_path = self.outbox_dir / filename

            record: dict[str, Any] = {
                "vendor_id": vendor["vendor_id"],
                "vendor_name": vendor["name"],
                "to": to_addr,
                "subject": subject,
                "rfx_id": rfx_id,
                "sent_at": sent_at,
                "status": None,
                "path": str(email_path),
            }

            # Always materialize the plaintext artifact so operators can audit.
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
            else:
                try:
                    msg = MIMEText(body, "plain", "utf-8")
                    msg["From"] = self.from_addr
                    msg["To"] = to_addr
                    msg["Subject"] = subject
                    with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                        server.sendmail(self.from_addr, [to_addr], msg.as_string())
                    record["status"] = "sent"
                except Exception as exc:  # noqa: BLE001 — surface as status
                    record["status"] = f"error: {exc}"

            records.append(record)
            self.dispatch_log.append(record)

        log_path = self.outbox_dir / f"dispatch_log_{_safe_slug(rfx_id)}.json"
        # Persist send records without duplicating full email bodies.
        log_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return paths  # email artifacts only; JSON log is side-effect


def dispatch_rfx(
    rfx: RFxLike,
    *,
    stub: bool = True,
    outbox_dir: str | os.PathLike | None = None,
) -> list[str]:
    """Module-level helper expected by agents.__init__."""
    return VendorDispatcherAgent(stub=stub, outbox_dir=outbox_dir).dispatch(rfx)


__all__ = ["VendorDispatcherAgent", "dispatch_rfx"]

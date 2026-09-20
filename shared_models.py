"""Shared Pydantic/dataclass contracts for the 5-agent RFx crew. Agents import from here."""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

class LineItem(BaseModel):
    line_id: str
    description: str
    qty: float
    uom: str = "piece"
    specs: dict[str, Any] = Field(default_factory=dict)

class QuestionnaireItem(BaseModel):
    id: str
    question: str
    knockout: bool = False

class Vendor(BaseModel):
    vendor_id: str
    name: str
    email: str

class RFx(BaseModel):
    rfx_id: str
    title: str
    scope: str
    terms: str
    currency: str = "INR"
    line_items: list[LineItem]
    questionnaire: list[QuestionnaireItem]
    vendors: list[Vendor]

class ExtractedQuote(BaseModel):
    vendor_id: str
    source_format: str
    lines: list[dict[str, Any]] = Field(default_factory=list)
    questionnaire_answers: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""
    confidence: float = 0.0
    raw_evidence: list[dict[str, Any]] = Field(default_factory=list)

class NormalizedCell(BaseModel):
    line_id: str
    vendor_id: str
    unit_price_inr: Optional[float] = None
    status: Literal["ok", "converted", "missing", "uncertain", "uom_mismatch"] = "missing"
    original_price: Optional[float] = None
    original_currency: Optional[str] = None
    original_uom: Optional[str] = None
    flags: list[str] = Field(default_factory=list)

class QuestionnaireResult(BaseModel):
    """Per-vendor result for one questionnaire item (esp. knockouts)."""
    question_id: str
    question: str
    knockout: bool = False
    answer: str = ""
    passed: Optional[bool] = None  # None = unanswered / not evaluated

class InboxMessage(BaseModel):
    """Stub inbound vendor email (or uploaded reply) awaiting parse."""
    msg_id: str
    vendor_id: str = ""
    vendor_name: str = ""
    subject: str = ""
    from_addr: str = ""
    path: str = ""
    body_preview: str = ""
    status: Literal["new", "parsed", "error"] = "new"
    error: str = ""
    parsed_vendor_id: str = ""

class KnockoutResult(BaseModel):
    question_id: str
    question: str
    answer: Optional[str] = None
    passed: bool
    reason: str = ""


class VendorQualification(BaseModel):
    vendor_id: str
    passed: bool
    award_eligible: bool
    knockout_results: list[KnockoutResult] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ComparisonTable(BaseModel):
    rfx_id: str
    cells: list[NormalizedCell]
    vendor_flags: dict[str, list[str]] = Field(default_factory=dict)
    qualifications: list[VendorQualification] = Field(default_factory=list)
    award_eligible_vendors: list[str] = Field(default_factory=list)
    # Wizard / analyst enrichment (optional; specialists may also set these)
    qualified_vendors: list[str] = Field(default_factory=list)
    questionnaire_results: dict[str, list[QuestionnaireResult]] = Field(default_factory=dict)
    vendor_names: dict[str, str] = Field(default_factory=dict)

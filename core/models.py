"""Pydantic models.

Two families live here:
- *AI output schemas* (suffix `AI`): exactly what the model is allowed to emit.
  They are deliberately narrow: the model reports what a document says and
  where it says it. It never computes normalized prices or totals.
- *Application state* models: what the app stores after deterministic
  post-processing (grounding, unit conversion, FX).
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# RFx
# ---------------------------------------------------------------------------

class Board(str, Enum):
    three_ply = "3-ply"
    five_ply = "5-ply"
    seven_ply = "7-ply"


class LineItemAI(BaseModel):
    line_no: int = Field(description="1-based sequential line number")
    sku: str = Field(description="Buyer SKU code, e.g. CB-RSC-001")
    description: str = Field(description="Short buyer description of the carton, e.g. 'RSC shipper, 5-ply, printed 1 colour'")
    board: Board
    flute: str = Field(description="Flute profile, e.g. 'B', 'C', 'BC', 'E'")
    length_mm: int
    width_mm: int
    height_mm: int
    gsm: int = Field(description="Total board grammage in g/m2, e.g. 450 for 3-ply, 750 for 5-ply")
    print: str = Field(description="Printing spec, e.g. 'Unprinted', '1 colour flexo', '2 colour flexo'")
    annual_qty: int = Field(description="Annual quantity in pieces")
    notes: str = Field(default="", description="Any special requirement (burst strength, moisture barrier, etc.)")


class CommercialTermsAI(BaseModel):
    currency: str = Field(default="INR")
    price_basis: str = Field(description="e.g. 'INR per piece, delivered to plant, exclusive of GST'")
    payment_terms: str
    delivery_location: str
    delivery_schedule: str
    freight_basis: str
    validity_days: int
    gst_treatment: str
    other: str = Field(default="")


class AnswerType(str, Enum):
    yes_no = "yes_no"
    text = "text"
    document = "document"


class QuestionAI(BaseModel):
    q_id: str = Field(description="Short id like Q1, Q2 ...")
    text: str
    answer_type: AnswerType
    knockout: bool = Field(description="True if a 'No' or missing answer disqualifies the vendor")


class RFxDraftAI(BaseModel):
    """A complete request-for-quotation for corrugated packaging."""

    title: str
    scope: str = Field(description="3-6 paragraphs: background, what is being sourced, volumes, quality expectations, submission instructions")
    line_items: list[LineItemAI] = Field(description="Exactly 30 line items")
    terms: CommercialTermsAI
    questionnaire: list[QuestionAI] = Field(description="8-12 quality and compliance questions; mark 3-4 as knockout")


# ---------------------------------------------------------------------------
# Extraction (what the model may say about a vendor document)
# ---------------------------------------------------------------------------

class PriceBasis(str, Enum):
    per_pc = "per_pc"
    per_100 = "per_100"
    per_1000 = "per_1000"
    per_kg = "per_kg"
    per_box = "per_box"
    per_bundle = "per_bundle"
    unknown = "unknown"


class ExtractStatus(str, Enum):
    ok = "ok"
    needs_review = "needs_review"
    unresolved = "unresolved"


class EvidenceAI(BaseModel):
    file_name: str = Field(description="Exact file name the evidence comes from")
    location: str = Field(description="Where in the file: e.g. 'Sheet1!D14', 'page 2, footnote', 'paragraph 6', 'table 1 row 4', 'line 3'")
    snippet: str = Field(description="VERBATIM text copied from the document, 5-200 characters, that contains the value. Do not paraphrase.")


class ReviewCause(str, Enum):
    none = "none"
    ambiguous_mapping = "ambiguous_mapping"
    illegible = "illegible"
    alternate_spec = "alternate_spec"
    spec_not_confirmed = "spec_not_confirmed"
    pack_size_unknown = "pack_size_unknown"
    currency_unknown = "currency_unknown"
    inferred_value = "inferred_value"
    conflicting_values = "conflicting_values"
    basis_differs_from_rfx = "basis_differs_from_rfx"
    refers_to_external_info = "refers_to_external_info"
    other = "other"


class LineQuoteAI(BaseModel):
    line_no: Optional[int] = Field(description="RFx line number this vendor row maps to, or null if unsure")
    candidate_line_nos: list[int] = Field(default_factory=list, description="If line_no is null or uncertain, the plausible RFx lines")
    vendor_item_ref: str = Field(default="", description="Vendor's own code / row label")
    vendor_description: str = Field(default="", description="Vendor's description of the item, verbatim if short")
    price: Optional[float] = Field(description="Numeric price exactly as written, in the vendor's currency and basis. Null if none stated.")
    currency: Optional[str] = Field(description="ISO code e.g. INR, USD. Null if not stated anywhere.")
    price_basis: PriceBasis
    basis_qty: Optional[float] = Field(default=None, description="For per_100 / per_1000 / per_bundle: the quantity the price covers")
    confidence: float = Field(ge=0, le=1)
    status: ExtractStatus
    review_cause: ReviewCause = Field(description="Primary cause when status is not ok; 'none' when ok. Use basis_differs_from_rfx when the ONLY concern is that the supplier's unit/currency/incoterm differs from the RFx.")
    reason: str = Field(default="", description="Why this is needs_review/unresolved, or empty")
    evidence: Optional[EvidenceAI]


class TermKey(str, Enum):
    payment_terms = "payment_terms"
    freight = "freight"
    gst = "gst"
    validity = "validity"
    discount = "discount"
    lead_time = "lead_time"
    moq = "moq"
    delivery = "delivery"
    other = "other"


class TermAI(BaseModel):
    key: TermKey
    value: str = Field(description="Plain statement of the term as the vendor gave it")
    numeric_pct: Optional[float] = Field(default=None, description="For discounts: percentage as a number, e.g. 5 for 5%")
    applies_to: str = Field(default="", description="Scope/condition, e.g. 'orders above Rs 25 lakh', 'all lines'")
    confidence: float = Field(ge=0, le=1)
    status: ExtractStatus
    evidence: Optional[EvidenceAI]


class QuestionnaireAnswerAI(BaseModel):
    q_id: str
    answer: str = Field(description="Vendor's answer: 'Yes', 'No', or short text. Empty string if not answered.")
    answered: bool
    confidence: float = Field(ge=0, le=1)
    status: ExtractStatus
    evidence: Optional[EvidenceAI]


class CertificateAI(BaseModel):
    name: str = Field(description="e.g. ISO 9001, FSC, BRC")
    attached_as_document: bool = Field(description="True only if a certificate document itself is among the files")
    claimed_in_text: bool = Field(description="True if the vendor merely states they hold it")
    evidence: Optional[EvidenceAI]


class ExtractionAI(BaseModel):
    """Everything the vendor's response says, with evidence for every value."""

    vendor_name_in_document: str = Field(default="")
    line_quotes: list[LineQuoteAI]
    not_quoted_line_nos: list[int] = Field(description="RFx lines the vendor did not price at all")
    commercials: list[TermAI]
    questionnaire: list[QuestionnaireAnswerAI]
    certificates: list[CertificateAI]
    notes: str = Field(default="", description="Anything a buyer should know that does not fit above")


# ---------------------------------------------------------------------------
# Image transcription
# ---------------------------------------------------------------------------

class ImageTranscriptionAI(BaseModel):
    """Faithful transcription of a photographed document."""

    transcription: str = Field(description="Line-by-line transcription. Use ' | ' between table columns. Mark unreadable characters with '?' and cut-off rows with '[cut off]'.")
    legibility: float = Field(ge=0, le=1, description="Overall legibility 0-1")
    caveats: str = Field(default="", description="Glare, skew, cropped rows, ambiguous digits")


# ---------------------------------------------------------------------------
# Clarification email
# ---------------------------------------------------------------------------

class ClarificationEmailAI(BaseModel):
    """A polite, specific clarification request to a vendor."""

    subject: str
    body: str

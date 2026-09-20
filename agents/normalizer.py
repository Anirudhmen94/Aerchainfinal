"""Deterministic quote normalisation.

This module deliberately contains no model client.  It turns extracted quote
records into a comparison table by applying only explicit currency and unit
conversions.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from shared_models import ComparisonTable, ExtractedQuote, NormalizedCell
from agents.qualification import award_eligible_vendors, build_questionnaire_results, eligibility_gaps, qualify_vendors, questionnaire_matrix, remap_questionnaire_answers


USD_TO_INR = 83.50

_PIECE_TERMS = {
    "piece", "pieces", "pc", "pcs", "each", "ea", "unit", "units",
    "per piece", "per pieces", "per pc", "per pcs", "per each", "per unit",
}

_AMBIGUOUS_TERMS = (
    "ambiguous", "uncertain", "unclear", "approx", "approximately",
    "estimated", "estimate", "tbd", "to be confirmed", "subject to confirmation",
    "same as last year", "as before", "refer attached", "not specified",
)


def _value(value: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dict or one of the shared Pydantic models."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(",", "")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _normalised_id(value: Any) -> str:
    text = _text(value)
    try:
        number = float(text)
        if number.is_integer():
            return str(int(number))
    except (TypeError, ValueError):
        pass
    return text


def _clean_description(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value).lower()).strip()


def _confidence_is_low(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return float(value) < 0.60
    text = _text(value).lower()
    if text in {"low", "none", "uncertain", "ambiguous", "needs review", "review"}:
        return True
    try:
        return float(text) < 0.60
    except ValueError:
        return False


def _contains_ambiguity(*values: Any) -> bool:
    for value in values:
        if isinstance(value, (list, tuple, set)):
            if _contains_ambiguity(*value):
                return True
        elif isinstance(value, dict):
            if _contains_ambiguity(*value.values()):
                return True
        elif any(term in _text(value).lower() for term in _AMBIGUOUS_TERMS):
            return True
    return False


def _uom_kind(uom: Any) -> tuple[str, float | None]:
    """Return (kind, pieces-per-quoted-unit) for a quote basis."""
    text = re.sub(r"\s+", " ", _text(uom).lower().replace("-", " ")).strip()
    if not text:
        return "piece", 1.0
    if text in _PIECE_TERMS:
        return "piece", 1.0
    quantity = re.search(r"(?:per\s*)?(\d+(?:\.\d+)?)\s*(?:pcs?|pieces?|units?|each)?\b", text)
    if quantity and float(quantity.group(1)) > 0:
        return "quantity", float(quantity.group(1))
    if "kilogram" in text or re.search(r"(?:^|\s|/)kg(?:$|\s|/)", text):
        return "kg", None
    if "box" in text:
        return "box", None
    if "bundle" in text:
        return "bundle", None
    if "pack" in text:
        return "pack", None
    if "roll" in text:
        return "roll", None
    if "sheet" in text:
        return "sheet", None
    return "unknown", None


def _specs(line: Any, rfx_line: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for source in (_value(rfx_line, "specs", {}), _value(line, "specs", {})):
        if isinstance(source, dict):
            out.update(source)
    return out


def _first_number(mapping: dict[str, Any], names: Iterable[str]) -> float | None:
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        number = _number(lowered.get(name.lower()))
        if number is not None and number > 0:
            return number
    return None


def _pack_size(line: Any, rfx_line: Any, specs: dict[str, Any]) -> float | None:
    names = (
        "basis_qty", "basis_quantity", "pack_size", "pack_qty", "pack_quantity",
        "pieces_per_box", "units_per_box", "pieces_per_pack", "units_per_pack",
        "pieces_per_bundle", "units_per_bundle", "box_qty", "bundle_qty",
    )
    number = _first_number(specs, names)
    if number is not None:
        return number
    for source in (line, rfx_line):
        for name in names:
            number = _number(_value(source, name))
            if number is not None and number > 0:
                return number
    uom = _text(_value(line, "uom", _value(line, "price_basis", ""))).lower()
    match = re.search(r"(?:box|bundle|pack)\s*(?:of|/)\s*(\d+(?:\.\d+)?)", uom)
    return float(match.group(1)) if match else None


def _weight_g(line: Any, rfx_line: Any, specs: dict[str, Any]) -> float | None:
    weight_g = _first_number(specs, ("nominal_weight_g", "piece_weight_g", "weight_g"))
    if weight_g is not None:
        return weight_g
    weight_kg = _first_number(specs, ("nominal_weight_kg", "piece_weight_kg", "weight_kg"))
    if weight_kg is not None:
        return weight_kg * 1000.0
    for source in (line, rfx_line):
        for name in ("nominal_weight_g", "piece_weight_g", "weight_g"):
            number = _number(_value(source, name))
            if number is not None and number > 0:
                return number
        for name in ("nominal_weight_kg", "piece_weight_kg", "weight_kg"):
            number = _number(_value(source, name))
            if number is not None and number > 0:
                return number * 1000.0
    return None


def _rfx_lines(rfx: Any) -> list[Any]:
    lines = _value(rfx, "line_items", [])
    return list(lines or [])


def _find_rfx_line(line: Any, rfx_lines: list[Any]) -> Any | None:
    requested = _value(line, "line_id")
    if requested is not None:
        wanted = _normalised_id(requested)
        for rfx_line in rfx_lines:
            if _normalised_id(_value(rfx_line, "line_id")) == wanted:
                return rfx_line
    description = _clean_description(_value(line, "description"))
    if description:
        exact = [item for item in rfx_lines if _clean_description(_value(item, "description")) == description]
        if len(exact) == 1:
            return exact[0]
    return None


def _vendor_id(extraction: Any) -> str:
    return _text(_value(extraction, "vendor_id")) or "unknown-vendor"


def _line_confidence(line: Any, extraction: Any) -> Any:
    value = _value(line, "confidence")
    return _value(extraction, "confidence", 0.0) if value is None else value


def _line_notes(line: Any, extraction: Any) -> list[str]:
    notes: list[str] = []
    for value in (_value(line, "notes"), _value(line, "note"), _value(extraction, "notes")):
        if value is not None and _text(value):
            notes.append(_text(value))
    return notes


def _price_and_currency(line: Any, rfx: Any) -> tuple[float | None, str]:
    price = _value(line, "unit_price")
    if price is None:
        price = _value(line, "price")
    if price is None:
        price = _value(line, "unit_price_inr")
    currency = _text(_value(line, "currency", _value(rfx, "currency", "INR"))) or "INR"
    return _number(price), currency.upper()


def _normalise_one_price(line: Any, rfx_line: Any, price: float, currency: str) -> tuple[float, str, list[str], bool, bool]:
    flags: list[str] = []
    converted = False
    mismatch = False
    original_uom = _text(_value(line, "uom", _value(line, "price_basis", "piece"))) or "piece"
    if currency == "USD":
        price *= USD_TO_INR
        converted = True
        flags.append(f"Converted USD to INR at {USD_TO_INR:.2f} INR/USD")
    elif currency not in {"INR", "RS", "₹"}:
        flags.append(f"Unsupported currency '{currency}'")
        mismatch = True
    kind, quantity = _uom_kind(original_uom)
    specs = _specs(line, rfx_line)
    if kind == "piece":
        pass
    elif kind == "quantity" and quantity:
        price /= quantity
        converted = True
        flags.append(f"UOM converted from {original_uom} to per piece (÷{quantity:g})")
    elif kind in {"box", "bundle", "pack"}:
        pack_size = _pack_size(line, rfx_line, specs)
        if pack_size:
            price /= pack_size
            converted = True
            flags.append(f"UOM converted from {original_uom} to per piece (÷{pack_size:g})")
        else:
            mismatch = True
            flags.append(f"UOM mismatch: cannot determine pieces in '{original_uom}'")
    elif kind == "kg":
        weight_g = _weight_g(line, rfx_line, specs)
        if weight_g:
            price *= weight_g / 1000.0
            converted = True
            flags.append(f"UOM converted from {original_uom} to per piece using {weight_g:g} g/piece")
        else:
            mismatch = True
            flags.append("UOM mismatch: per-kg quote has no nominal piece weight")
    else:
        mismatch = True
        flags.append(f"UOM mismatch: cannot convert '{original_uom}' to per piece")
    return price, original_uom, flags, converted, mismatch


def normalize(rfx, extractions: list[ExtractedQuote]) -> ComparisonTable:
    """Normalize extracted quotes into one deterministic comparison table."""
    rfx_lines = _rfx_lines(rfx)
    cells: list[NormalizedCell] = []
    vendor_flags: dict[str, list[str]] = {}
    default_currency = _text(_value(rfx, "currency", "INR")).upper() or "INR"
    for extraction in extractions:
        vendor_id = _vendor_id(extraction)
        flags: list[str] = []
        raw_lines = _value(extraction, "lines", []) or []
        matched: dict[str, Any] = {}
        for line in raw_lines:
            rfx_line = _find_rfx_line(line, rfx_lines)
            if rfx_line is None:
                description = _text(_value(line, "description", _value(line, "line_id", "unknown")))
                flags.append(f"Could not match quoted line '{description}' to RFx")
                continue
            rid = _normalised_id(_value(rfx_line, "line_id"))
            if rid not in matched:
                matched[rid] = line
        for rfx_line in rfx_lines:
            line_id = _normalised_id(_value(rfx_line, "line_id"))
            line = matched.get(line_id)
            if line is None:
                cells.append(NormalizedCell(line_id=line_id, vendor_id=vendor_id, status="missing", flags=["Not quoted"]))
                flags.append(f"Missing RFx line {line_id}")
                continue
            price, currency = _price_and_currency(line, rfx)
            currency = _text(_value(line, "currency", default_currency)).upper() or default_currency
            confidence = _line_confidence(line, extraction)
            notes = _line_notes(line, extraction)
            cell_flags: list[str] = []
            if price is None:
                cell_flags.append("Missing or invalid unit price")
                flags.append(f"Invalid price for line {line_id}")
                cells.append(NormalizedCell(line_id=line_id, vendor_id=vendor_id, status="uncertain", flags=cell_flags))
                continue
            price_inr, original_uom, conversion_flags, converted, mismatch = _normalise_one_price(line, rfx_line, price, currency)
            cell_flags.extend(conversion_flags)
            cell_flags.extend(notes)
            low_confidence = _confidence_is_low(confidence)
            ambiguous = _contains_ambiguity(*notes)
            if low_confidence:
                cell_flags.append(f"Low confidence: {confidence}")
                flags.append(f"Low confidence on line {line_id}")
            if ambiguous:
                cell_flags.append("Ambiguous note requires review")
                flags.append(f"Ambiguous note on line {line_id}")
            if mismatch:
                status = "uom_mismatch"
                flags.append(f"UOM mismatch on line {line_id}")
            elif low_confidence or ambiguous:
                status = "uncertain"
            elif converted:
                status = "converted"
            else:
                status = "ok"
            cells.append(NormalizedCell(
                line_id=line_id,
                vendor_id=vendor_id,
                unit_price_inr=round(price_inr, 2),
                status=status,
                original_price=price,
                original_currency=currency,
                original_uom=original_uom,
                flags=cell_flags,
            ))
        vendor_flags[vendor_id] = list(dict.fromkeys(flags))

    qualifications = qualify_vendors(rfx, extractions)
    for qual in qualifications:
        vf = vendor_flags.setdefault(qual.vendor_id, [])
        if qual.award_eligible:
            vf.append("questionnaire_passed")
        else:
            vf.append("questionnaire_failed")
            vf.extend(qual.reasons)
        vendor_flags[qual.vendor_id] = list(dict.fromkeys(vf))

    award_eligible = [q.vendor_id for q in qualifications if q.award_eligible]
    # Full question×vendor results (knockouts + non-knockouts) for Compare matrix.
    q_results = build_questionnaire_results(rfx, extractions)
    vendor_names = {
        _text(_value(v, "vendor_id")): _text(_value(v, "name"))
        for v in (_value(rfx, "vendors", []) or [])
        if _text(_value(v, "vendor_id"))
    }
    return ComparisonTable(
        rfx_id=_text(_value(rfx, "rfx_id")),
        cells=cells,
        vendor_flags=vendor_flags,
        qualifications=qualifications,
        award_eligible_vendors=award_eligible,
        qualified_vendors=list(award_eligible),
        questionnaire_results=q_results,
        vendor_names=vendor_names,
    )


class NormalizerAgent:
    """Thin compatibility wrapper around the function-first API."""
    def __init__(self, client=None, usd_to_inr: float = USD_TO_INR):
        self.client = client
        self.usd_to_inr = USD_TO_INR

    def normalize(self, rfx, extractions: list[ExtractedQuote]) -> ComparisonTable:
        return normalize(rfx, extractions)


normalize_quotes = normalize

__all__ = ["USD_TO_INR", "NormalizerAgent", "normalize", "normalize_quotes", "qualify_vendors", "award_eligible_vendors", "questionnaire_matrix", "build_questionnaire_results", "eligibility_gaps", "remap_questionnaire_answers"]

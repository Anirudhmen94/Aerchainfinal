#!/usr/bin/env python3
"""Generate the five assignment ugly-edge vendor reply binaries into data/vendor_responses/.

Run from repo root:
  python data/fixtures/generate_ugly_edges.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "vendor_responses"
FIX = Path(__file__).resolve().parent

# Shared questionnaire (knockouts-friendly wording)
Q_COMMON = [
    {"id": "Q1", "question": "Is the manufacturing site currently ISO 9001 certified?", "answer": "Yes"},
    {"id": "Q2", "question": "Standard manufacturing lead time (calendar days) for this basket?", "answer": "14"},
    {"id": "Q3", "question": "Can you support VMI / consignment for top 10 SKUs?", "answer": "Yes, subject to forecast"},
    {"id": "Q4", "question": "Minimum order quantity per SKU?", "answer": "500 ea"},
    {"id": "Q5", "question": "Can you supply FSC-certified board on request?", "answer": "Yes, +3%"},
    {"id": "Q6", "question": "Defect / rejection rate over the last 12 months (%)?", "answer": "0.5"},
    {"id": "Q7", "question": "Do you accept returns for verified quality failures within 14 days?", "answer": "Yes"},
]

# Catalog of 30 lines with base piece rates (INR) for constructing weird UOMs
CATALOG = [
    ("L01", "3-ply RSC carton 300x200x150 mm", 18.50),
    ("L02", "3-ply RSC carton 400x300x200 mm", 26.00),
    ("L03", "3-ply RSC carton 500x400x300 mm", 37.50),
    ("L04", "5-ply RSC carton 300x200x150 mm", 28.00),
    ("L05", "5-ply RSC carton 400x300x200 mm", 40.00),
    ("L06", "5-ply RSC carton 500x400x300 mm", 55.00),
    ("L07", "7-ply heavy RSC 600x500x400 mm", 120.00),
    ("L08", "7-ply heavy RSC 800x600x500 mm", 175.00),
    ("L09", "3-ply die-cut mailer 250x200x50 mm", 14.00),
    ("L10", "5-ply die-cut mailer 350x250x80 mm", 21.50),
    ("L11", "Corrugated pallet tray 1200x800 mm", 85.00),
    ("L12", "Corner guard L-profile 600 mm", 4.20),
    ("L13", "Corner guard L-profile 900 mm", 5.80),
    ("L14", "3-ply corrugated sheet 1200x800 mm", 11.50),
    ("L15", "5-ply corrugated sheet 1200x800 mm", 17.20),
    ("L16", "Corrugated tube OD50 x 300 mm", 8.50),
    ("L17", "Corrugated tube OD75 x 300 mm", 13.00),
    ("L18", "6-cell partition insert 3-ply", 95.00),
    ("L19", "12-cell partition insert 3-ply", 140.00),
    ("L20", "24-cell partition insert 5-ply", 210.00),
    ("L21", "Telescopic outer 400x300x200 3-ply", 65.00),
    ("L22", "Telescopic inner 400x300x200 3-ply", 60.00),
    ("L23", "Archive box 400x300x250 3-ply", 52.00),
    ("L24", "Archive box 400x300x250 5-ply", 72.00),
    ("L25", "Kraft paper tape 48mm x 100m", 38.00),
    ("L26", "Bubble wrap 1.2m x 50m", 420.00),
    ("L27", "PE foam sheet 5mm 1200x800", 35.00),
    ("L28", "PP strapping band 12mm", 185.00),
    ("L29", "Shrink film 500mm x 300m", 860.00),
    ("L30", "Wooden pallet 1200x800 EUR", 640.00),
]


def _write_extract(name: str, payload: dict) -> Path:
    path = OUT / f"{Path(name).stem}.extract.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def gen_v01_xlsx() -> Path:
    """Excel that ignores any buyer template — weird columns, per-100 pcs."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "OUR_RATES_v3"
    # Deliberately weird / non-template headers
    headers = [
        "SKU_CODE",
        "BUYER_REF_MAYBE",
        "WHAT_WE_CALL_IT",
        "COMMERCIAL_RATE",
        "BASIS",
        "CCY",
        "MOQ_NOTE",
        "INTERNAL_ONLY",
    ]
    ws.append(headers)
    for col in range(1, 9):
        cell = ws.cell(1, col)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E79")

    # Mix of per-100 and ea; skip L08 (capacity) to show a gap; weird SKU codes
    skip = {"L08"}
    for lid, desc, piece in CATALOG:
        if lid in skip:
            continue
        # Most cartons quoted per 100 pcs (ignore piece template)
        if int(lid[1:]) <= 15:
            rate = round(piece * 100, 2)
            basis = "INR per 100 pcs"
        else:
            rate = piece
            basis = "ea"
        ws.append([
            f"PF-{lid[-2:]}-X",
            lid,  # buried buyer ref in column 2, not "line_id"
            desc.upper(),
            rate,
            basis,
            "INR",
            "MOQ 500" if "carton" in desc.lower() else "",
            "do-not-map",
        ])

    # Blank + questionnaire sheet (answers alongside numbers)
    wq = wb.create_sheet("QNA_DONT_RENAME")
    wq.append(["PROMPT_TEXT", "OUR_REPLY", "KO"])
    for q in Q_COMMON:
        wq.append([q["question"], q["answer"], "Y" if q["id"] in {"Q1", "Q5", "Q7"} else "N"])

    path = OUT / "V01_ignore_template.xlsx"
    wb.save(path)

    lines = []
    for lid, desc, piece in CATALOG:
        if lid in skip:
            continue
        if int(lid[1:]) <= 15:
            lines.append({
                "line_id": lid,
                "description": desc,
                "unit_price": round(piece * 100, 2),
                "uom": "per 100 pcs",
                "currency": "INR",
                "notes": "Quoted ignoring buyer piece template",
            })
        else:
            lines.append({
                "line_id": lid,
                "description": desc,
                "unit_price": piece,
                "uom": "ea",
                "currency": "INR",
                "notes": "",
            })
    _write_extract(
        "V01_ignore_template.xlsx",
        {
            "vendor_id": "V01",
            "vendor_name": "PackForge India",
            "currency": "INR",
            "source_format": "xlsx",
            "notes": "Excel ignores buyer template; many lines priced per 100 pcs. L08 not quoted (capacity).",
            "confidence": 0.92,
            "lines": lines,
            "questionnaire_answers": Q_COMMON,
            "raw_evidence": [{"snippet": "BASIS=INR per 100 pcs", "location": "OUR_RATES_v3!E"}],
        },
    )
    return path


def gen_v02_pdf() -> Path:
    """PDF letterhead; ~27/30 lines; discount buried in footnote."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    path = OUT / "V02_letterhead_footnote.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4

    # Letterhead
    c.setFillColorRGB(0.05, 0.2, 0.35)
    c.rect(0, h - 28 * mm, w, 28 * mm, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(18 * mm, h - 14 * mm, "CARTONWORKS LLP")
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, h - 20 * mm, "Corrugated Packaging · Pune · rfq@cartonworks.in")
    c.setFillColorRGB(0, 0, 0)

    y = h - 38 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(18 * mm, y, "Quotation — RFX-CORR-2026-001")
    y -= 6 * mm
    c.setFont("Helvetica", 9)
    c.drawString(18 * mm, y, "Validity 45 days · Net 45 · GST extra · DDP Pune DC unless noted")
    y -= 8 * mm

    # Only 27 of 30 lines (omit L16, L17, L28)
    omit = {"L16", "L17", "L28"}
    lines_out = []
    c.setFont("Helvetica", 8.5)
    for lid, desc, piece in CATALOG:
        if lid in omit:
            continue
        # Slightly aggressive rates
        rate = round(piece * 0.98, 2)
        row = f"{lid}  {desc[:48]:<48}  Rs {rate:>7.2f} / ea"
        c.drawString(18 * mm, y, row)
        lines_out.append({
            "line_id": lid,
            "description": desc,
            "unit_price": rate,
            "uom": "ea",
            "currency": "INR",
            "notes": "",
        })
        y -= 5.2 * mm
        if y < 32 * mm:
            break

    # Footnote with buried discount (small type, bottom)
    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    footnote = (
        "* Footnote 3: For annual volumes above ₹40 lakh a discretionary settlement discount of "
        "3.5% may be applied at invoice (not reflected in unit rates above). Tubes L16/L17 and "
        "PP strap L28 withheld pending tooling clearance — 27 of 30 lines quoted."
    )
    # wrap
    words = footnote.split()
    line = ""
    fy = 22 * mm
    for word in words:
        trial = (line + " " + word).strip()
        if c.stringWidth(trial, "Helvetica", 7) > w - 36 * mm:
            c.drawString(18 * mm, fy, line)
            fy -= 3.2 * mm
            line = word
        else:
            line = trial
    if line:
        c.drawString(18 * mm, fy, line)

    c.setFont("Helvetica-Oblique", 8)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(18 * mm, 10 * mm, "Questionnaire attached as Annex-Q (ISO Yes · LT 11d · VMI No · FSC No · Defects 0.9%).")
    c.save()

    answers = [
        {"id": "Q1", "question": "Is the manufacturing site currently ISO 9001 certified?", "answer": "Yes"},
        {"id": "Q2", "question": "Standard manufacturing lead time (calendar days) for this basket?", "answer": "11"},
        {"id": "Q3", "question": "Can you support VMI / consignment for top 10 SKUs?", "answer": "No — not at this time"},
        {"id": "Q4", "question": "Minimum order quantity per SKU?", "answer": "1000 ea"},
        {"id": "Q5", "question": "Can you supply FSC-certified board on request?", "answer": "Not available"},
        {"id": "Q6", "question": "Defect / rejection rate over the last 12 months (%)?", "answer": "0.9"},
        {"id": "Q7", "question": "Do you accept returns for verified quality failures within 14 days?", "answer": "Credit note within 10 days"},
    ]
    _write_extract(
        "V02_letterhead_footnote.pdf",
        {
            "vendor_id": "V02",
            "vendor_name": "CartonWorks LLP",
            "currency": "INR",
            "source_format": "pdf",
            "notes": (
                "Only 27/30 lines quoted (L16, L17, L28 missing). "
                "3.5% settlement discount buried in footnote — not in unit rates."
            ),
            "confidence": 0.88,
            "lines": lines_out,
            "questionnaire_answers": answers,
            "raw_evidence": [
                {"snippet": "discretionary settlement discount of 3.5%", "location": "footnote"},
                {"snippet": "27 of 30 lines quoted", "location": "footnote"},
            ],
        },
    )
    return path


def gen_v03_docx() -> Path:
    """Word .docx with commercials in a prose paragraph (USD / per 1000)."""
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches

    doc = Document()
    title = doc.add_heading("PacificBoard Inc — Commercial Offer", level=1)
    p = doc.add_paragraph()
    run = p.add_run("Ref: PB/RFX/2026-CORR · Currency: United States Dollars (USD)")
    run.bold = True

    doc.add_paragraph(
        "Dear Procurement Team,"
    )
    # Single prose paragraph with USD / per 1000 — the ugly edge
    prose = (
        "We are pleased to offer the following commercial package for RFX-CORR-2026-001. "
        "All unit rates below are expressed in USD per 1,000 pieces (not per piece): "
        "L01 three-ply RSC 300×200×150 at USD 215.00 per thousand; "
        "L02 at USD 305.00 per thousand; L03 at USD 440.00 per thousand; "
        "L04 five-ply small at USD 330.00 / 1000; L05 USD 475.00 / 1000; L06 USD 650.00 / 1000; "
        "L07 seven-ply heavy USD 1,380.00 per thousand and L08 USD 2,010.00 per thousand; "
        "mailers L09/L10 USD 160.00 and USD 248.00 per thousand respectively; "
        "pallet tray L11 USD 980.00 per thousand; corner guards L12 USD 48.00 and L13 USD 66.00 per thousand; "
        "sheets L14/L15 USD 132.00 / USD 198.00 per thousand sheets; "
        "tubes L16/L17 USD 98.00 / USD 150.00 per thousand; "
        "partitions L18–L20 USD 1,100 / 1,620 / 2,420 per thousand; "
        "telescopic L21/L22 USD 750 / 690 per thousand; archive L23/L24 USD 600 / 830 per thousand; "
        "consumables quoted per thousand sell-units: kraft tape L25 USD 440, bubble L26 USD 4,850, "
        "foam L27 USD 405, PP strap L28 USD 2.20 per kg (weight basis — not per thousand), "
        "shrink L29 USD 9,900, wooden pallet L30 USD 7.40 each (exception — each, not per thousand). "
        "Freight to Pune is extra; FX settlement on invoice date. Offer valid 30 days."
    )
    doc.add_paragraph(prose)

    doc.add_heading("Questionnaire responses", level=2)
    for q in [
        ("Q1 ISO 9001", "Yes (site PB-CHN-02)"),
        ("Q2 Lead time", "16 calendar days"),
        ("Q3 VMI", "Yes for top 8 SKUs after 3 months"),
        ("Q4 MOQ", "750 ea"),
        ("Q5 FSC", "Available, +5%"),
        ("Q6 Defect rate", "0.55%"),
        ("Q7 Returns", "Yes within 14 days if QC failure documented"),
    ]:
        doc.add_paragraph(f"{q[0]} — {q[1]}")

    doc.add_paragraph("Regards,\nPriya Nair\nPacificBoard Inc\ntenders@pacificboard.com")

    path = OUT / "V03_prose_commercials.docx"
    doc.save(path)

    # Companion with per-1000 USD numbers (L28 kg, L30 each flagged)
    usd_map = {
        "L01": 215.0, "L02": 305.0, "L03": 440.0, "L04": 330.0, "L05": 475.0,
        "L06": 650.0, "L07": 1380.0, "L08": 2010.0, "L09": 160.0, "L10": 248.0,
        "L11": 980.0, "L12": 48.0, "L13": 66.0, "L14": 132.0, "L15": 198.0,
        "L16": 98.0, "L17": 150.0, "L18": 1100.0, "L19": 1620.0, "L20": 2420.0,
        "L21": 750.0, "L22": 690.0, "L23": 600.0, "L24": 830.0, "L25": 440.0,
        "L26": 4850.0, "L27": 405.0, "L29": 9900.0,
    }
    lines = []
    for lid, desc, _ in CATALOG:
        if lid == "L28":
            lines.append({
                "line_id": "L28",
                "description": desc,
                "unit_price": 2.20,
                "uom": "kg",
                "currency": "USD",
                "notes": "Weight basis — UOM mismatch vs piece",
            })
        elif lid == "L30":
            lines.append({
                "line_id": "L30",
                "description": desc,
                "unit_price": 7.40,
                "uom": "ea",
                "currency": "USD",
                "notes": "Quoted each, not per 1000",
            })
        elif lid in usd_map:
            lines.append({
                "line_id": lid,
                "description": desc,
                "unit_price": usd_map[lid],
                "uom": "per 1000",
                "currency": "USD",
                "notes": "USD vendor — convert FX + divide by 1000",
            })

    _write_extract(
        "V03_prose_commercials.docx",
        {
            "vendor_id": "V03",
            "vendor_name": "PacificBoard Inc",
            "currency": "USD",
            "source_format": "docx",
            "notes": "USD vendor. Commercials buried in prose as USD per 1000 pieces. Freight extra. L28 is per kg.",
            "confidence": 0.85,
            "lines": lines,
            "questionnaire_answers": [
                {"id": "Q1", "question": "Is the manufacturing site currently ISO 9001 certified?", "answer": "Yes (site PB-CHN-02)"},
                {"id": "Q2", "question": "Standard manufacturing lead time (calendar days) for this basket?", "answer": "16"},
                {"id": "Q3", "question": "Can you support VMI / consignment for top 10 SKUs?", "answer": "Yes for top 8 SKUs after 3 months"},
                {"id": "Q4", "question": "Minimum order quantity per SKU?", "answer": "750 ea"},
                {"id": "Q5", "question": "Can you supply FSC-certified board on request?", "answer": "Available, +5%"},
                {"id": "Q6", "question": "Defect / rejection rate over the last 12 months (%)?", "answer": "0.55"},
                {"id": "Q7", "question": "Do you accept returns for verified quality failures within 14 days?", "answer": "Yes within 14 days if QC failure documented"},
            ],
            "raw_evidence": [
                {"snippet": "USD per 1,000 pieces (not per piece)", "location": "paragraph 2"},
                {"snippet": "PP strap L28 USD 2.20 per kg", "location": "paragraph 2"},
            ],
        },
    )
    return path


def gen_v04_png() -> Path:
    """Angled phone-photo style PNG of a printed rate card (per-box / per bundle)."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    # Draw a "printed" rate card then rotate/skew onto a desk background
    card_w, card_h = 900, 1200
    card = Image.new("RGB", (card_w, card_h), (252, 248, 235))
    draw = ImageDraw.Draw(card)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        font_b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        font_s = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except Exception:
        font = font_b = font_s = ImageFont.load_default()

    draw.rectangle([20, 20, card_w - 20, card_h - 20], outline=(30, 30, 30), width=3)
    draw.text((50, 40), "QUICKCORR — RATE CARD", fill=(20, 20, 80), font=font_b)
    draw.text((50, 80), "RFX-CORR-2026-001  |  prices per BOX / per BUNDLE", fill=(60, 60, 60), font=font_s)
    draw.text((50, 110), "(phone photo of desk printout — do not retype)", fill=(120, 80, 40), font=font_s)

    # Rates as per-box (assume 50 pcs/box) or per-bundle (20 pcs)
    y = 160
    rate_rows = [
        ("L01", "3-ply 300x200x150", "₹925 / box (50 pcs)"),
        ("L02", "3-ply 400x300x200", "₹1,300 / box (50)"),
        ("L03", "3-ply 500x400x300", "₹1,875 / box"),
        ("L04", "5-ply 300x200x150", "₹1,400 / box"),
        ("L05", "5-ply 400x300x200", "₹2,000 / box"),
        ("L06", "5-ply 500x400x300", "₹2,750 / box"),
        ("L07", "7-ply heavy 600", "₹2,400 / bundle (20)"),
        ("L08", "7-ply heavy 800", "₹3,500 / bundle (20)"),
        ("L09", "mailer 3-ply", "₹700 / box (50)"),
        ("L10", "mailer 5-ply", "₹1,075 / box"),
        ("L11", "pallet tray", "₹85 ea (not boxed)"),
        ("L12", "corner 600mm", "₹84 / bundle (20)"),
        ("L13", "corner 900mm", "₹116 / bundle"),
        ("L18", "6-cell partition", "₹1,900 / box (20)"),
        ("L19", "12-cell partition", "₹2,800 / box"),
        ("L20", "24-cell partition", "₹4,200 / box"),
        ("L25", "kraft tape", "₹38 / roll"),
        ("L30", "EUR pallet", "₹640 ea"),
    ]
    for lid, label, rate in rate_rows:
        draw.text((50, y), f"{lid}  {label}", fill=(20, 20, 20), font=font)
        draw.text((520, y), rate, fill=(20, 60, 20), font=font)
        y += 36

    draw.text((50, y + 20), "Q: ISO Yes · LT 12d · VMI case-by-case · MOQ 400 · FSC Yes", fill=(40, 40, 40), font=font_s)
    draw.text((50, y + 50), "Missing lines = not on this card (ask for formal sheet)", fill=(140, 40, 40), font=font_s)
    draw.text((50, card_h - 60), "QuickCorr Pvt Ltd · sales@quickcorr.in", fill=(80, 80, 80), font=font_s)

    # Soft blur + rotate onto larger canvas to look like angled phone photo
    card = card.filter(ImageFilter.SMOOTH)
    angled = card.rotate(12, expand=True, fillcolor=(90, 95, 100))
    canvas = Image.new("RGB", (1200, 1600), (70, 75, 82))
    # desk texture stripes
    desk = ImageDraw.Draw(canvas)
    for i in range(0, 1600, 8):
        desk.line([(0, i), (1200, i)], fill=(65 + (i % 3), 70, 78))
    # paste angled card with shadow
    ox, oy = 80, 120
    shadow = Image.new("RGBA", angled.size, (0, 0, 0, 0))
    # simple offset paste
    canvas.paste(angled, (ox + 12, oy + 12))
    canvas.paste(angled, (ox, oy))
    # vignette-ish darken corners via overlay
    path = OUT / "V04_rate_card_photo.png"
    canvas.save(path, "PNG")

    lines = []
    # Convert known box/bundle quotes to structured form with pack sizes
    structured = [
        ("L01", "3-ply RSC carton 300x200x150 mm", 925, "per box", 50),
        ("L02", "3-ply RSC carton 400x300x200 mm", 1300, "per box", 50),
        ("L03", "3-ply RSC carton 500x400x300 mm", 1875, "per box", 50),
        ("L04", "5-ply RSC carton 300x200x150 mm", 1400, "per box", 50),
        ("L05", "5-ply RSC carton 400x300x200 mm", 2000, "per box", 50),
        ("L06", "5-ply RSC carton 500x400x300 mm", 2750, "per box", 50),
        ("L07", "7-ply heavy RSC 600x500x400 mm", 2400, "per bundle", 20),
        ("L08", "7-ply heavy RSC 800x600x500 mm", 3500, "per bundle", 20),
        ("L09", "3-ply die-cut mailer 250x200x50 mm", 700, "per box", 50),
        ("L10", "5-ply die-cut mailer 350x250x80 mm", 1075, "per box", 50),
        ("L11", "Corrugated pallet tray 1200x800 mm", 85, "ea", 1),
        ("L12", "Corner guard L-profile 600 mm", 84, "per bundle", 20),
        ("L13", "Corner guard L-profile 900 mm", 116, "per bundle", 20),
        ("L18", "6-cell partition insert 3-ply", 1900, "per box", 20),
        ("L19", "12-cell partition insert 3-ply", 2800, "per box", 20),
        ("L20", "24-cell partition insert 5-ply", 4200, "per box", 20),
        ("L25", "Kraft paper tape 48mm x 100m", 38, "roll", 1),
        ("L30", "Wooden pallet 1200x800 EUR", 640, "ea", 1),
    ]
    for lid, desc, price, uom, pack in structured:
        lines.append({
            "line_id": lid,
            "description": desc,
            "unit_price": price,
            "uom": uom,
            "currency": "INR",
            "notes": f"Rate card photo; pack_size={pack}" if pack > 1 else "Rate card photo",
            "pack_size": pack,
            "pieces_per_box": pack if "box" in uom else None,
            "pieces_per_bundle": pack if "bundle" in uom else None,
        })

    _write_extract(
        "V04_rate_card_photo.png",
        {
            "vendor_id": "V04",
            "vendor_name": "QuickCorr Pvt Ltd",
            "currency": "INR",
            "source_format": "png",
            "notes": "Angled phone photo of printed rate card. Many lines per-box/per-bundle; several RFx lines missing from card.",
            "confidence": 0.72,
            "lines": lines,
            "questionnaire_answers": [
                {"id": "Q1", "question": "Is the manufacturing site currently ISO 9001 certified?", "answer": "Yes"},
                {"id": "Q2", "question": "Standard manufacturing lead time (calendar days) for this basket?", "answer": "12"},
                {"id": "Q3", "question": "Can you support VMI / consignment for top 10 SKUs?", "answer": "Case-by-case"},
                {"id": "Q4", "question": "Minimum order quantity per SKU?", "answer": "400"},
                {"id": "Q5", "question": "Can you supply FSC-certified board on request?", "answer": "Yes on request"},
                {"id": "Q6", "question": "Defect / rejection rate over the last 12 months (%)?", "answer": "0.6"},
                {"id": "Q7", "question": "Do you accept returns for verified quality failures within 14 days?", "answer": "Yes, 14 days"},
            ],
            "raw_evidence": [
                {"snippet": "₹925 / box (50 pcs)", "location": "rate card photo L01"},
                {"snippet": "₹2,400 / bundle (20)", "location": "rate card photo L07"},
            ],
        },
    )
    return path


def gen_v05_email() -> Path:
    """One-line email style reply."""
    body = (
        "From: Ravi <bid@nestpack.in>\n"
        "To: procurement@buyer.example\n"
        "Subject: Re: RFX-CORR-2026-001\n"
        "Date: Mon, 15 Sep 2026 16:04:00 +0530\n"
        "\n"
        "₹42/kg for the 5-ply, 38 for the 3-ply, rest same as last year, freight extra.\n"
    )
    path = OUT / "V05_oneline_email.txt"
    path.write_text(body, encoding="utf-8")

    # Companion: uncertain / same-as-last-year — only kg board rates + flags
    _write_extract(
        "V05_oneline_email.txt",
        {
            "vendor_id": "V05",
            "vendor_name": "NestPack Industries",
            "currency": "INR",
            "source_format": "email",
            "notes": (
                "One-line email. Board quoted per kg (3-ply ₹38/kg, 5-ply ₹42/kg) — "
                "NOT piece rates. 'rest same as last year' is uncertain. Freight extra."
            ),
            "confidence": 0.40,
            "lines": [
                {
                    "line_id": "L01",
                    "description": "3-ply board basket (indicative)",
                    "unit_price": 38.0,
                    "uom": "kg",
                    "currency": "INR",
                    "notes": "per-kg board rate — cannot convert to piece without weight; uncertain",
                },
                {
                    "line_id": "L04",
                    "description": "5-ply board basket (indicative)",
                    "unit_price": 42.0,
                    "uom": "kg",
                    "currency": "INR",
                    "notes": "per-kg board rate — uom_mismatch vs piece",
                },
            ],
            "questionnaire_answers": [
                {"id": "Q1", "question": "Is the manufacturing site currently ISO 9001 certified?", "answer": ""},
                {"id": "Q2", "question": "Standard manufacturing lead time (calendar days) for this basket?", "answer": ""},
            ],
            "raw_evidence": [
                {
                    "snippet": "₹42/kg for the 5-ply, 38 for the 3-ply, rest same as last year, freight extra.",
                    "location": "email body",
                }
            ],
        },
    )
    return path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    written = [
        gen_v01_xlsx(),
        gen_v02_pdf(),
        gen_v03_docx(),
        gen_v04_png(),
        gen_v05_email(),
    ]
    print("Wrote:")
    for p in written:
        print(f"  {p.relative_to(ROOT)} ({p.stat().st_size} bytes)")
        ex = OUT / f"{p.stem}.extract.json"
        if ex.exists():
            print(f"  {ex.relative_to(ROOT)} (companion extract)")


if __name__ == "__main__":
    main()

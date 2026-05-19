"""
Formal asset document PDF generator — Lupine AMS document language system.

Production requirement for Polish character support:
    apt install fonts-liberation
    OR: apt install fonts-dejavu-core

Without a TTF font, Helvetica is used as fallback (limited Polish chars).
"""
import io
import os
from datetime import date, datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# ---------------------------------------------------------------------------
# Font registration — cross-platform, full Polish character support.
#
# Priority order per OS:
#   Linux (prod): fonts-liberation or fonts-dejavu-core (apt install)
#   Windows (dev): Arial / Calibri / Consolas — all support full Polish
#   macOS: Arial via standard font dirs
#
# Fallback: Helvetica / Courier (built-in PDF fonts, NO Polish chars).
# ---------------------------------------------------------------------------

_REGULAR_CANDIDATES = [
    # Linux — Liberation Sans (recommended: apt install fonts-liberation)
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    # Linux — DejaVu Sans (apt install fonts-dejavu-core)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    # Linux — FreeSans fallback
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    # Windows — Arial (full Polish Unicode, confirmed)
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/Arial.ttf",
    # Windows — Calibri (full Polish Unicode, confirmed)
    "C:/Windows/Fonts/calibri.ttf",
    # Windows — Segoe UI (full Polish Unicode, confirmed)
    "C:/Windows/Fonts/segoeui.ttf",
    # macOS — Arial
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

_BOLD_CANDIDATES = [
    # Linux — Liberation Sans Bold
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    # Linux — DejaVu Sans Bold
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    # Linux — FreeSans Bold
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    # Windows — Arial Bold
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/Arialbd.ttf",
    # Windows — Calibri Bold
    "C:/Windows/Fonts/calibrib.ttf",
    # Windows — Segoe UI Bold
    "C:/Windows/Fonts/segoeuib.ttf",
    # macOS — Arial Bold
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

_MONO_CANDIDATES = [
    # Linux — Liberation Mono
    "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    # Linux — DejaVu Sans Mono
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    # Windows — Consolas (excellent Unicode coverage incl. Polish)
    "C:/Windows/Fonts/consola.ttf",
    # Windows — Courier New (ASCII-range mono, adequate for codes/dates)
    "C:/Windows/Fonts/cour.ttf",
    "C:/Windows/Fonts/Cour.ttf",
    # macOS
    "/Library/Fonts/Courier New.ttf",
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
]

_BODY_FONT = "Helvetica"
_BODY_FONT_BOLD = "Helvetica-Bold"
_MONO_FONT = "Courier"


def _try_register(name, candidates):
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                return True
            except Exception:
                continue
    return False


if _try_register("DocSans", _REGULAR_CANDIDATES):
    _BODY_FONT = "DocSans"
if _try_register("DocSans-Bold", _BOLD_CANDIDATES):
    _BODY_FONT_BOLD = "DocSans-Bold"
if _try_register("DocMono", _MONO_CANDIDATES):
    _MONO_FONT = "DocMono"


# ---------------------------------------------------------------------------
# Palette — strict monochrome per language system spec
# ---------------------------------------------------------------------------

_C_BLACK = colors.HexColor("#111111")
_C_DARK = colors.HexColor("#444444")
_C_MID = colors.HexColor("#888888")
_C_RULE = colors.HexColor("#999999")
_C_HDR_BG = colors.HexColor("#e8e8e8")   # header row fill
_C_SUM_BG = colors.HexColor("#eeeeee")   # summary row fill
_C_BORDER_OUTER = 0.75                   # pt — outer table border
_C_BORDER_INNER = 0.4                    # pt — inner grid lines
_C_BORDER_HEAVY = 0.75                   # pt — header separator

# Page metrics — portrait A4 (LT documents)
_PAGE_W, _PAGE_H = A4
_MARGIN_L = 20 * mm
_MARGIN_R = 15 * mm
_MARGIN_T = 15 * mm
_MARGIN_B = 18 * mm   # larger bottom to make room for footer
_CONTENT_W = _PAGE_W - _MARGIN_L - _MARGIN_R   # 175mm
_FOOTER_H = 8 * mm

# Page metrics — landscape A4 (depreciation report)
_LAND_W, _LAND_H = landscape(A4)
_LAND_ML = 15 * mm
_LAND_MR = 15 * mm
_LAND_MT = 12 * mm
_LAND_MB = 16 * mm
_LAND_CW = _LAND_W - _LAND_ML - _LAND_MR   # 267mm


# ---------------------------------------------------------------------------
# Paragraph style factory
# ---------------------------------------------------------------------------

def _ps(
    name, size,
    bold=False, mono=False, color=None,
    alignment=0, leading=None,
    space_before=0, space_after=0,
):
    if mono:
        font = _MONO_FONT
    else:
        font = _BODY_FONT_BOLD if bold else _BODY_FONT
    return ParagraphStyle(
        name=name,
        fontName=font,
        fontSize=size,
        leading=leading or size * 1.35,
        alignment=alignment,
        textColor=color or _C_BLACK,
        spaceBefore=space_before,
        spaceAfter=space_after,
    )


# ---------------------------------------------------------------------------
# Per-page canvas callbacks — header strip + footer on every page
# ---------------------------------------------------------------------------

def _draw_page_frame(canvas, doc):
    """Called on every page — draws the footer band."""
    canvas.saveState()
    ts = getattr(doc, "_lt_generated_at_str", "")
    doc_num = getattr(doc, "_lt_doc_number", "")
    page_num = canvas.getPageNumber()
    total_pages = getattr(doc, "_lt_total_pages", "?")

    # Footer separator line
    y_line = _MARGIN_B - 5 * mm
    canvas.setStrokeColor(_C_RULE)
    canvas.setLineWidth(0.3)
    canvas.line(_MARGIN_L, y_line, _PAGE_W - _MARGIN_R, y_line)

    # Footer text — two parts: left and right
    canvas.setFont(_MONO_FONT, 6.5)
    canvas.setFillColor(_C_MID)

    left_text = f"Lupine AMS  ·  {doc_num}  ·  Wygenerowano: {ts}"
    right_text = f"Strona {page_num} z {total_pages}"

    canvas.drawString(_MARGIN_L, y_line - 4.5 * mm, left_text)
    canvas.drawRightString(_PAGE_W - _MARGIN_R, y_line - 4.5 * mm, right_text)

    canvas.restoreState()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_lt_pdf(
    assets,
    org,
    date_of_action: date,
    generated_by,
    generated_at: datetime,
    issuing_unit_text: str = "",
    notes: str = "",
    doc_number: str = "",
) -> io.BytesIO:
    """
    Generate an LT formal liquidation document as an immutable PDF.

    Args:
        assets:             iterable of Asset instances
        org:                OrganizationSettings instance
        date_of_action:     date of the actual liquidation operation
        generated_by:       User who triggered generation
        generated_at:       datetime of PDF creation
        issuing_unit_text:  optional branch / unit name printed below org name
        notes:              optional remarks / legal basis
        doc_number:         optional document reference number (e.g. LT/000001/2026)

    Returns:
        BytesIO buffer at position 0 containing the PDF.
    """
    assets = list(assets)

    buffer = io.BytesIO()

    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=_MARGIN_L,
        rightMargin=_MARGIN_R,
        topMargin=_MARGIN_T,
        bottomMargin=_MARGIN_B,
        title="LT — Likwidacja środka trwałego",
        author=_operator_display(generated_by),
    )

    # Attach metadata for footer callback
    doc._lt_generated_at_str = generated_at.strftime("%Y-%m-%d %H:%M") if generated_at else ""
    doc._lt_doc_number = doc_number or "LT/—"
    doc._lt_total_pages = "?"   # patched after build via two-pass

    frame = Frame(
        _MARGIN_L, _MARGIN_B,
        _CONTENT_W, _PAGE_H - _MARGIN_T - _MARGIN_B,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    template = PageTemplate(id="main", frames=[frame], onPage=_draw_page_frame)
    doc.addPageTemplates([template])

    story = _build_story(
        assets, org, date_of_action, generated_by,
        generated_at, issuing_unit_text, notes, doc_number,
    )

    # Two-pass build for accurate page count in footer
    doc.build(story)
    total = doc.page
    doc._lt_total_pages = total

    buffer.seek(0)
    # Re-build with correct page count
    buffer2 = io.BytesIO()
    doc2 = BaseDocTemplate(
        buffer2,
        pagesize=A4,
        leftMargin=_MARGIN_L,
        rightMargin=_MARGIN_R,
        topMargin=_MARGIN_T,
        bottomMargin=_MARGIN_B,
        title="LT — Likwidacja środka trwałego",
        author=_operator_display(generated_by),
    )
    doc2._lt_generated_at_str = doc._lt_generated_at_str
    doc2._lt_doc_number = doc._lt_doc_number
    doc2._lt_total_pages = total
    frame2 = Frame(
        _MARGIN_L, _MARGIN_B,
        _CONTENT_W, _PAGE_H - _MARGIN_T - _MARGIN_B,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    template2 = PageTemplate(id="main", frames=[frame2], onPage=_draw_page_frame)
    doc2.addPageTemplates([template2])
    story2 = _build_story(
        assets, org, date_of_action, generated_by,
        generated_at, issuing_unit_text, notes, doc_number,
    )
    doc2.build(story2)
    buffer2.seek(0)
    return buffer2


# ---------------------------------------------------------------------------
# Story builder
# ---------------------------------------------------------------------------

def _build_story(assets, org, date_of_action, generated_by, generated_at, issuing_unit_text, notes, doc_number):
    story = []

    # ==========================================================================
    # 1. DOCUMENT HEADER — two-column: org identity (left) | doc ref (right)
    # ==========================================================================
    story.append(_header_table(org, date_of_action, generated_at, issuing_unit_text, doc_number))
    story.append(Spacer(1, 3 * mm))

    # ==========================================================================
    # 2. DOCUMENT TITLE
    # ==========================================================================
    story.append(_title_block())
    story.append(Spacer(1, 4 * mm))

    # ==========================================================================
    # 3. FORMAL CONTEXT — operator, doc counts
    # ==========================================================================
    story.append(_context_table(assets, generated_by))
    story.append(Spacer(1, 5 * mm))

    # ==========================================================================
    # 4. SECTION LABEL — "WYKAZ ŚRODKÓW"
    # ==========================================================================
    story.append(_section_label("WYKAZ ŚRODKÓW OBJĘTYCH LIKWIDACJĄ"))
    story.append(Spacer(1, 1 * mm))

    # ==========================================================================
    # 5. MAIN ASSET TABLE
    # ==========================================================================
    story.append(_asset_table(assets))
    story.append(Spacer(1, 5 * mm))

    # ==========================================================================
    # 6. NOTES / LEGAL BASIS
    # ==========================================================================
    story.append(_section_label("PODSTAWA LIKWIDACJI / UWAGI"))
    story.append(Spacer(1, 1 * mm))
    story.append(_notes_block(notes))
    story.append(Spacer(1, 6 * mm))

    # ==========================================================================
    # 7. PLACE, DATE, SIGNATURES
    # ==========================================================================
    story.append(_place_date_line())
    story.append(Spacer(1, 6 * mm))
    story.append(_signature_table())

    return story


# ---------------------------------------------------------------------------
# Section: header
# ---------------------------------------------------------------------------

def _header_table(org, date_of_action, generated_at, issuing_unit_text, doc_number):
    """
    Two-column header:
      LEFT  — organization name + unit
      RIGHT — document number, dates (in a bordered box)
    """
    org_full = (getattr(org, "full_name", "") or "").strip()
    org_short = (getattr(org, "short_name", "") or "").strip()
    footer_text = (getattr(org, "report_footer", "") or "").strip()

    # Left cell content
    left_parts = []
    if org_full:
        left_parts.append(Paragraph(org_full, _ps("hdr_org", 11, bold=True)))
    if org_short and org_short != org_full:
        left_parts.append(Paragraph(org_short, _ps("hdr_short", 8.5, color=_C_DARK)))
    if issuing_unit_text:
        left_parts.append(Paragraph(issuing_unit_text, _ps("hdr_unit", 8.5, color=_C_DARK)))
    if footer_text:
        left_parts.append(Paragraph(footer_text, _ps("hdr_footer", 7.5, color=_C_MID)))
    if not left_parts:
        left_parts.append(Paragraph("—", _ps("hdr_empty", 8.5, color=_C_MID)))

    # Right cell — doc number + dates in a compact layout
    action_str = date_of_action.strftime("%d.%m.%Y") if date_of_action else "—"
    gen_str = generated_at.strftime("%d.%m.%Y") if generated_at else "—"
    num_str = doc_number or "LT/—"

    right_rows = [
        [Paragraph("Nr dokumentu:", _ps("rh_lbl", 7, color=_C_MID)),
         Paragraph(num_str, _ps("rh_num", 9, bold=True, mono=True))],
        [Paragraph("Data czynności:", _ps("rh_lbl2", 7, color=_C_MID)),
         Paragraph(action_str, _ps("rh_act", 9, mono=True))],
        [Paragraph("Data wystawienia:", _ps("rh_lbl3", 7, color=_C_MID)),
         Paragraph(gen_str, _ps("rh_gen", 9, mono=True))],
    ]
    right_tbl = Table(right_rows, colWidths=[28 * mm, 32 * mm])
    right_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("BOX", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
        ("INNERGRID", (0, 0), (-1, -1), _C_BORDER_INNER, _C_RULE),
    ]))

    # Left content width: 175 - 60mm (right box) - 5mm gap = 110mm
    left_content = "\n".join("")  # placeholder
    outer_rows = [[left_parts, right_tbl]]
    outer_tbl = Table(
        outer_rows,
        colWidths=[115 * mm, 60 * mm],
        rowHeights=None,
    )
    outer_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, -1), 5),  # gap between left and right
        ("RIGHTPADDING", (1, 0), (1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
    ]))
    return outer_tbl


# ---------------------------------------------------------------------------
# Section: title
# ---------------------------------------------------------------------------

def _title_block():
    rows = [
        [Paragraph("LT", _ps("t_abbr", 8, bold=True, color=_C_MID, alignment=1))],
        [Paragraph("LIKWIDACJA ŚRODKA TRWAŁEGO / WYPOSAŻENIA", _ps("t_main", 12, bold=True, alignment=1))],
    ]
    tbl = Table(rows, colWidths=[_CONTENT_W])
    tbl.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEABOVE", (0, 0), (-1, 0), _C_BORDER_OUTER, _C_BLACK),
        ("LINEBELOW", (0, -1), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Section: formal context
# ---------------------------------------------------------------------------

def _context_table(assets, generated_by):
    """Compact formal-data block: operator, asset count."""
    operator = _operator_display(generated_by)
    count = str(len(assets))

    lbl = _ps("ctx_lbl", 7.5, bold=True, color=_C_DARK)
    val = _ps("ctx_val", 8.5)

    rows = [
        [Paragraph("DANE FORMALNE", _ps("ctx_hdr", 7.5, bold=True, alignment=0)),
         "", "", ""],
        [Paragraph("Dokument sporządził:", lbl),
         Paragraph(operator, val),
         Paragraph("Liczba pozycji:", lbl),
         Paragraph(count, val)],
        [Paragraph("Komisja likwidacyjna:", lbl),
         Paragraph(" ", val),
         Paragraph("Przewodniczący komisji:", lbl),
         Paragraph(" ", val)],
        [Paragraph("Podstawa prawna:", lbl),
         Paragraph(" ", val),
         "", ""],
    ]

    col_w = [40 * mm, 47.5 * mm, 40 * mm, 47.5 * mm]
    tbl = Table(rows, colWidths=col_w)
    tbl.setStyle(TableStyle([
        # Header row spanning all columns
        ("SPAN", (0, 0), (-1, 0)),
        ("BACKGROUND", (0, 0), (-1, 0), _C_HDR_BG),
        ("FONT", (0, 0), (-1, 0), _BODY_FONT_BOLD, 7.5),
        ("TOPPADDING", (0, 0), (-1, 0), 3),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        ("LEFTPADDING", (0, 0), (-1, 0), 4),
        # Last row: span columns 2-3
        ("SPAN", (1, 3), (3, 3)),
        # Global
        ("BOX", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
        ("INNERGRID", (0, 1), (-1, -1), _C_BORDER_INNER, _C_RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        ("LEFTPADDING", (0, 1), (-1, -1), 4),
        ("RIGHTPADDING", (0, 1), (-1, -1), 4),
        # Operator row: underlines for hand-writable cells
        ("LINEBELOW", (1, 2), (1, 2), 0.4, _C_RULE),
        ("LINEBELOW", (3, 2), (3, 2), 0.4, _C_RULE),
        ("LINEBELOW", (1, 3), (3, 3), 0.4, _C_RULE),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Section: label
# ---------------------------------------------------------------------------

def _section_label(text: str):
    rows = [[Paragraph(text, _ps("sec_lbl", 7.5, bold=True, color=_C_DARK))]]
    tbl = Table(rows, colWidths=[_CONTENT_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _C_HDR_BG),
        ("BOX", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Section: asset table
# ---------------------------------------------------------------------------

def _asset_table(assets):
    """
    Main data table.
    Columns (sum = 175mm):
      Lp.  Nr inw.  Kod kreskowy  Nazwa             Rodzaj  Szt.  Lokalizacja  Stan
      8    26       22            37                 22      9     32           19
    """
    col_widths = [8 * mm, 26 * mm, 22 * mm, 37 * mm, 22 * mm, 9 * mm, 32 * mm, 19 * mm]

    th = _ps("th", 7.5, bold=True, alignment=1)   # table header
    td = _ps("td", 7.5)                            # body left
    tr = _ps("tr_", 7.5, alignment=2)              # body right (numbers)
    tm = _ps("tm", 7.5, mono=True)                 # mono for codes

    header = [
        Paragraph("Lp.", th),
        Paragraph("Nr inw.", th),
        Paragraph("Kod kreskowy", th),
        Paragraph("Nazwa środka", th),
        Paragraph("Rodzaj", th),
        Paragraph("Szt.", th),
        Paragraph("Lokalizacja", th),
        Paragraph("Stan", th),
    ]

    rows = [header]
    for i, asset in enumerate(assets, 1):
        rows.append([
            Paragraph(str(i), tr),
            Paragraph(asset.inventory_number or "—", tm),
            Paragraph(asset.barcode or "—", tm),
            Paragraph(asset.name or "—", td),
            Paragraph(_asset_type_display(asset), td),
            Paragraph(str(asset.current_quantity), tr),
            Paragraph(asset.location or "—", td),
            Paragraph(asset.get_status_display() if hasattr(asset, "get_status_display") else "—", td),
        ])

    if len(rows) == 1:
        rows.append([Paragraph("—", tr)] + [Paragraph("Brak pozycji do wyświetlenia", td)] + [""] * 6)

    # Summary row
    total_qty = sum(getattr(a, "current_quantity", 1) for a in assets)
    sum_row = [
        Paragraph("", th),
        Paragraph("RAZEM:", _ps("sum_lbl", 7.5, bold=True)),
        "", "", "",
        Paragraph(str(total_qty), _ps("sum_qty", 7.5, bold=True, alignment=2)),
        "", "",
    ]
    rows.append(sum_row)

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)

    n_data = len(rows) - 2     # rows excluding header and summary
    n_last = len(rows) - 1     # summary row index

    tbl.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), _C_HDR_BG),
        ("FONT", (0, 0), (-1, 0), _BODY_FONT_BOLD, 7.5),
        ("LINEBELOW", (0, 0), (-1, 0), _C_BORDER_HEAVY, _C_BLACK),
        # Summary row
        ("BACKGROUND", (0, n_last), (-1, n_last), _C_SUM_BG),
        ("LINEABOVE", (0, n_last), (-1, n_last), _C_BORDER_HEAVY, _C_BLACK),
        ("SPAN", (1, n_last), (4, n_last)),   # merge label columns
        ("SPAN", (6, n_last), (7, n_last)),   # merge trailing columns
        # Outer box: heavy
        ("BOX", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
        # Inner grid: light
        ("INNERGRID", (0, 0), (-1, -1), _C_BORDER_INNER, _C_RULE),
        # Padding (tight — formal doc)
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Section: notes
# ---------------------------------------------------------------------------

def _notes_block(notes: str):
    """Bordered textarea with notes text or blank lines if empty."""
    content_style = _ps("notes_body", 8.5, space_after=2 * mm)
    line_style = _ps("notes_line", 8, color=_C_MID)

    if notes and notes.strip():
        inner = [Paragraph(notes.strip(), content_style)]
        # Pad to minimum height with blank lines
        inner += [Paragraph(" ", line_style)] * 2
    else:
        inner = [Paragraph(" ", line_style)] * 4

    # Wrap in a bordered box
    tbl = Table([[inner]], colWidths=[_CONTENT_W])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Section: place / date line
# ---------------------------------------------------------------------------

def _place_date_line():
    rows = [[
        Paragraph("Miejscowość, dnia:", _ps("pd_lbl", 8, bold=True)),
        Paragraph(" ", _ps("pd_val", 8)),
    ]]
    tbl = Table(rows, colWidths=[38 * mm, 80 * mm])
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (1, 0), (1, 0), 0.5, _C_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Section: signatures + seal
# ---------------------------------------------------------------------------

def _signature_table():
    """
    Formal signature section.

    Layout (175mm total):
      Sporządził(a)  | gap | Zatwierdził(a)  | gap | M.P. (seal)
      55mm             5mm   55mm              5mm   55mm
    """
    # Row 0: labels (above the empty space)
    # Row 1: empty space for hand-written content
    # Row 2: underline row
    # Row 3: signer identification labels (below the line)

    lbl = _ps("sig_top_lbl", 7, bold=True, color=_C_DARK, alignment=1)
    sub = _ps("sig_sub_lbl", 7, color=_C_MID, alignment=1)
    seal = _ps("seal_lbl", 7, color=_C_MID, alignment=1)
    empty = _ps("sig_empty", 8)

    data = [
        # Row 0 — role labels at top of signature block
        [
            Paragraph("Sporządził / Sporządziła", lbl),
            "",
            Paragraph("Zatwierdził / Zatwierdziła", lbl),
            "",
            Paragraph("M.P.", seal),
        ],
        # Row 1 — empty space (hand signature)
        ["", "", "", "", ""],
        # Row 2 — signature lines (visual via LINEABOVE on row 3)
        ["", "", "", "", ""],
        # Row 3 — sub-labels: name/title
        [
            Paragraph("(podpis i pieczęć imienna)", sub),
            "",
            Paragraph("(podpis i pieczęć imienna)", sub),
            "",
            Paragraph("(pieczęć organizacji)", sub),
        ],
    ]

    col_w = [55 * mm, 5 * mm, 55 * mm, 5 * mm, 55 * mm]
    row_h = [5 * mm, 18 * mm, 0.5 * mm, 5 * mm]

    tbl = Table(data, colWidths=col_w, rowHeights=row_h)
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        # Signature underlines (top of row 2 = bottom of row 1 signature space)
        ("LINEBELOW", (0, 1), (0, 1), 0.6, _C_BLACK),
        ("LINEBELOW", (2, 1), (2, 1), 0.6, _C_BLACK),
        # Seal box
        ("BOX", (4, 0), (4, 3), 0.5, _C_RULE),
        ("SPAN", (4, 0), (4, 3)),
        ("VALIGN", (4, 0), (4, 3), "BOTTOM"),
        # Padding
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Depreciation report — public API
# ---------------------------------------------------------------------------

def generate_depreciation_report_pdf(
    rows,
    totals,
    period_label: str,
    org,
    generated_at: datetime,
) -> io.BytesIO:
    """
    Generate a depreciation report PDF in landscape A4.

    Args:
        rows:         list of row dicts from build_depreciation_report()
        totals:       totals dict from build_depreciation_report()
        period_label: human-readable period string (e.g. "Maj 2026")
        org:          OrganizationSettings instance
        generated_at: datetime of generation

    Returns:
        BytesIO buffer at position 0.
    """
    ts = generated_at.strftime("%Y-%m-%d %H:%M") if generated_at else ""

    def _make_doc(buf):
        d = BaseDocTemplate(
            buf,
            pagesize=landscape(A4),
            leftMargin=_LAND_ML,
            rightMargin=_LAND_MR,
            topMargin=_LAND_MT,
            bottomMargin=_LAND_MB,
            title="Zestawienie odpisów amortyzacyjnych",
        )
        d._rpt_ts = ts
        d._rpt_period = period_label
        d._rpt_total_pages = "?"
        frame = Frame(
            _LAND_ML, _LAND_MB,
            _LAND_CW, _LAND_H - _LAND_MT - _LAND_MB,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )
        d.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_draw_rpt_frame)])
        return d

    story = _build_rpt_story(rows, totals, period_label, org, ts)

    buf1 = io.BytesIO()
    doc1 = _make_doc(buf1)
    doc1.build(story)
    total_pages = doc1.page

    buf2 = io.BytesIO()
    doc2 = _make_doc(buf2)
    doc2._rpt_total_pages = total_pages
    doc2.build(_build_rpt_story(rows, totals, period_label, org, ts))
    buf2.seek(0)
    return buf2


def _draw_rpt_frame(canvas, doc):
    canvas.saveState()
    y_line = _LAND_MB - 5 * mm
    canvas.setStrokeColor(_C_RULE)
    canvas.setLineWidth(0.3)
    canvas.line(_LAND_ML, y_line, _LAND_W - _LAND_MR, y_line)
    canvas.setFont(_MONO_FONT, 6.5)
    canvas.setFillColor(_C_MID)
    left_text = (
        f"Lupine AMS  ·  Zestawienie odpisów amortyzacyjnych  ·  "
        f"{getattr(doc, '_rpt_period', '')}  ·  Wygenerowano: {getattr(doc, '_rpt_ts', '')}"
    )
    right_text = f"Strona {canvas.getPageNumber()} z {getattr(doc, '_rpt_total_pages', '?')}"
    canvas.drawString(_LAND_ML, y_line - 4.5 * mm, left_text)
    canvas.drawRightString(_LAND_W - _LAND_MR, y_line - 4.5 * mm, right_text)
    canvas.restoreState()


def _build_rpt_story(rows, totals, period_label, org, ts):
    story = []

    org_full = (getattr(org, "full_name", "") or "").strip()

    header_data = [[
        Paragraph(org_full or "—", _ps("rh_org", 10, bold=True)),
        Paragraph("ZESTAWIENIE ODPISÓW AMORTYZACYJNYCH", _ps("rh_ttl", 11, bold=True, alignment=1)),
        Paragraph(period_label, _ps("rh_per", 10, bold=True, alignment=2)),
    ]]
    hdr_tbl = Table(header_data, colWidths=[90 * mm, 110 * mm, 67 * mm])
    hdr_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 4 * mm))
    story.append(_rpt_data_table(rows, totals))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"Wygenerowano: {ts}  ·  Liczba pozycji: {len(rows)}",
        _ps("rh_meta", 7, color=_C_MID),
    ))
    return story


def _rpt_data_table(rows, totals):
    # Column widths summing to 267mm (_LAND_CW)
    col_widths = [w * mm for w in [7, 23, 45, 20, 18, 21, 21, 20, 14, 12, 22, 22, 22]]

    th = _ps("rth", 7, bold=True, alignment=1)
    td = _ps("rtd", 7)
    tr_ = _ps("rtr", 7, alignment=2)
    tm = _ps("rtm", 7, mono=True)

    header = [
        Paragraph("Lp.", th),
        Paragraph("Nr inw.", th),
        Paragraph("Nazwa środka", th),
        Paragraph("KST", th),
        Paragraph("Data\nstartu", th),
        Paragraph("Wartość\npoczątkowa", th),
        Paragraph("Wartość\nrezydualna", th),
        Paragraph("Podstawa\namort.", th),
        Paragraph("Metoda", th),
        Paragraph("Stawka\n%", th),
        Paragraph("Odpis za\nokres (zł)", th),
        Paragraph("Skumulowana\namort. (zł)", th),
        Paragraph("Wartość\nnetto (zł)", th),
    ]

    data = [header]
    for i, row in enumerate(rows, 1):
        rate = f"{row['annual_rate_percent']:.2f}" if row["annual_rate_percent"] else "—"
        data.append([
            Paragraph(str(i), tr_),
            Paragraph(row["inventory_number"], tm),
            Paragraph(row["name"], td),
            Paragraph(row["kst_category"], td),
            Paragraph(row["start_date"].strftime("%Y-%m-%d"), tm),
            Paragraph(f"{row['initial_value']:.2f}", tr_),
            Paragraph(f"{row['residual_value']:.2f}", tr_),
            Paragraph(f"{row['depreciation_base']:.2f}", tr_),
            Paragraph(row["method_display"], td),
            Paragraph(rate, tr_),
            Paragraph(f"{row['period_charge']:.2f}", tr_),
            Paragraph(f"{row['accumulated']:.2f}", tr_),
            Paragraph(f"{row['net_value']:.2f}", tr_),
        ])

    if len(data) == 1:
        data.append([Paragraph("—", tr_), Paragraph("Brak danych dla wybranego okresu", td)] + [""] * 11)

    n_last = len(data)
    sum_row = [
        Paragraph("", th),
        Paragraph("RAZEM:", _ps("rs_lbl", 7, bold=True)),
        "", "", "",
        Paragraph(f"{totals['initial_value']:.2f}", _ps("rs_iv", 7, bold=True, alignment=2)),
        "",
        Paragraph(f"{totals['depreciation_base']:.2f}", _ps("rs_db", 7, bold=True, alignment=2)),
        "", "",
        Paragraph(f"{totals['period_charge']:.2f}", _ps("rs_pc", 7, bold=True, alignment=2)),
        Paragraph(f"{totals['accumulated']:.2f}", _ps("rs_ac", 7, bold=True, alignment=2)),
        Paragraph(f"{totals['net_value']:.2f}", _ps("rs_nv", 7, bold=True, alignment=2)),
    ]
    data.append(sum_row)

    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _C_HDR_BG),
        ("FONT", (0, 0), (-1, 0), _BODY_FONT_BOLD, 7),
        ("LINEBELOW", (0, 0), (-1, 0), _C_BORDER_HEAVY, _C_BLACK),
        ("BACKGROUND", (0, n_last), (-1, n_last), _C_SUM_BG),
        ("LINEABOVE", (0, n_last), (-1, n_last), _C_BORDER_HEAVY, _C_BLACK),
        ("SPAN", (1, n_last), (4, n_last)),
        ("BOX", (0, 0), (-1, -1), _C_BORDER_OUTER, _C_BLACK),
        ("INNERGRID", (0, 0), (-1, -1), _C_BORDER_INNER, _C_RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return tbl


# ---------------------------------------------------------------------------
# LT document helpers
# ---------------------------------------------------------------------------

def _operator_display(user) -> str:
    if user is None:
        return "—"
    full_name = (user.get_full_name().strip() if hasattr(user, "get_full_name") else "")
    return full_name or (user.username if hasattr(user, "username") else str(user))


def _asset_type_display(asset) -> str:
    if hasattr(asset, "asset_type_ref") and asset.asset_type_ref:
        return asset.asset_type_ref.name or "—"
    if hasattr(asset, "get_asset_type_display"):
        return asset.get_asset_type_display() or "—"
    return "—"

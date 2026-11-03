"""Vector Code128 location labels and A4 scanning sheets."""
import io
import math

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from assets.documents import _BODY_FONT, _BODY_FONT_BOLD


def _fit(text, font, size, width):
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + "…", font, size) > width:
        text = text[:-1]
    return text + "…"


def _lines(c, text, x, y, width, font, size, limit=2):
    lines = simpleSplit(text, font, size, width)
    if len(lines) > limit:
        lines = lines[:limit - 1] + [_fit(" ".join(lines[limit - 1:]), font, size, width)]
    c.setFont(font, size)
    for index, line in enumerate(lines):
        c.drawString(x, y - index * size * 1.15, line)


def _barcode(c, code, x, y, width, height, fill_width=False):
    barcode = Code128(code, barWidth=0.33 * mm, barHeight=height,
                      quiet=True, lquiet=3.3 * mm, rquiet=3.3 * mm,
                      humanReadable=False)
    scale = width / barcode.width if fill_width else min(1, width / barcode.width)
    c.saveState()
    c.translate(x + (width - barcode.width * scale) / 2, y)
    c.scale(scale, 1)
    barcode.drawOn(c, 0, 0)
    c.restoreState()


def generate_location_labels_pdf(locations, organization):
    """One 50 x 30 mm page per location, using the existing location code."""
    buffer = io.BytesIO()
    width, height = 50 * mm, 30 * mm
    c = canvas.Canvas(buffer, pagesize=(width, height))
    c.setTitle("Etykiety lokalizacji")
    for location in locations:
        c.setFont(_BODY_FONT, 7)
        c.drawCentredString(width / 2, 27.5 * mm, _fit(organization, _BODY_FONT, 7, 47 * mm))
        _lines(c, location.name, 1.5 * mm, 24 * mm, 47 * mm, _BODY_FONT_BOLD, 9)
        _barcode(c, location.code, 0.75 * mm, 4.5 * mm,
                 48.5 * mm, 14 * mm, fill_width=True)
        c.setFont("Courier", min(9, 9 * (47 * mm) / max(1, stringWidth(location.code, "Courier", 9))))
        c.drawCentredString(width / 2, 1.2 * mm, location.code)
        c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def generate_location_sheet_pdf(locations, organization, session_number):
    """Twelve isolated scanning targets per A4 page, ordered by location path."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Kody lokalizacji - {session_number}")
    width, height = A4
    margin, gap = 12 * mm, 4 * mm
    card_width = (width - 2 * margin - gap) / 2
    card_height = 40 * mm
    page_count = max(1, math.ceil(len(locations) / 12))
    for page in range(page_count):
        c.setFont(_BODY_FONT_BOLD, 14)
        c.drawString(margin, height - 16 * mm, "Kody lokalizacji / pomieszczeń")
        c.setFont(_BODY_FONT, 9)
        subtitle = f"{organization} | {session_number} | Lokalizacje: {len(locations)}"
        c.drawString(margin, height - 22 * mm, _fit(subtitle, _BODY_FONT, 9, width - 2 * margin))
        c.setFont(_BODY_FONT, 8)
        c.drawString(margin, height - 27 * mm, "Zeskanuj kod pomieszczenia, a następnie kody środków w tym miejscu.")
        for index, location in enumerate(locations[page * 12:(page + 1) * 12]):
            row, col = divmod(index, 2)
            x = margin + col * (card_width + gap)
            top = height - 32 * mm - row * card_height
            c.setStrokeColorRGB(0.78, 0.78, 0.78)
            c.setLineWidth(0.4)
            c.rect(x, top - card_height + mm, card_width, card_height - 2 * mm)
            _lines(c, location.name, x + 3 * mm, top - 5.5 * mm,
                   card_width - 6 * mm, _BODY_FONT_BOLD, 11)
            parent = location.parent.path if location.parent_id else "Lokalizacja główna"
            c.setFont(_BODY_FONT, 7)
            c.drawString(x + 3 * mm, top - 15 * mm, _fit(parent, _BODY_FONT, 7, card_width - 6 * mm))
            _barcode(c, location.code, x + 3 * mm, top - 32 * mm, card_width - 6 * mm, 14 * mm)
            c.setFont("Courier", 10)
            c.drawCentredString(x + card_width / 2, top - 36.5 * mm, location.code)
        c.setFont(_BODY_FONT, 8)
        c.drawString(margin, 12 * mm, session_number)
        c.drawRightString(width - margin, 12 * mm, f"Strona {page + 1} / {page_count}")
        c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

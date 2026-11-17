"""A4 binder register: one location per page, vector Code128 symbols."""
import io
from itertools import groupby
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode.code128 import Code128
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from xml.sax.saxutils import escape
from .documents import _BODY_FONT, _BODY_FONT_BOLD


def _symbol(value):
    if not value or any(ord(ch) < 32 or ord(ch) > 126 for ch in value):
        raise ValueError("Kod nie może być wydrukowany jako Code128: " + repr(value))
    symbol = Code128(value, barWidth=.32 * mm, barHeight=12 * mm,
                     quiet=True, lquiet=3.2 * mm, rquiet=3.2 * mm)
    if symbol.width * (.19 / .32) > 180 * mm:
        raise ValueError("Kod jest zbyt długi do czytelnego wydruku A4: " + value)
    return symbol


def _draw_symbol(c, value, y):
    symbol = _symbol(value)
    scale = min(1, 180 * mm / symbol.width)
    c.saveState()
    c.translate((A4[0] - symbol.width * scale) / 2, y)
    c.scale(scale, 1)
    symbol.drawOn(c, 0, 0)
    c.restoreState()


def _text(c, text, y, *, bold=False, size=9, max_height=10):
    style = ParagraphStyle('catalogue', fontName=_BODY_FONT_BOLD if bold else _BODY_FONT,
                           fontSize=size, leading=size+2)
    p = Paragraph(escape(str(text)), style)
    _, height = p.wrap(184 * mm, max_height * mm)
    if height > max_height * mm:
        raise ValueError("Tekst wymaga więcej miejsca na wydruku: " + str(text))
    p.drawOn(c, 13 * mm, y - height)


def generate_catalogued_pdf(assets, organization):
    assets = sorted(assets, key=lambda a: (a.location_fk_id or 0, a.barcode, a.pk))
    pages = []
    for _, grouped in groupby(assets, key=lambda a: a.location_fk_id):
        group = list(grouped)
        for offset in range(0, len(group), 6):
            pages.append(group[offset:offset+6])
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle("Ewidencja katalogowanych środków")
    for page_number, rows in enumerate(pages, 1):
        location = rows[0].location_fk
        _text(c, organization, 285 * mm, bold=True, size=11, max_height=12)
        _text(c, "Ewidencja katalogowanych środków", 270 * mm, bold=True, size=14)
        label = location.path if location else rows[0].location or "Bez przypisanej lokalizacji"
        _text(c, "Lokalizacja: " + label, 259 * mm, bold=True, max_height=16)
        if location and location.code:
            _draw_symbol(c, location.code, 226 * mm)
            _text(c, "Kod lokalizacji: " + location.code, 223 * mm)
        instruction = ("Najpierw zeskanuj lokalizację, następnie kody potwierdzonych przedmiotów."
                       if location and location.code else
                       "Wybierz rzeczywistą lokalizację na terminalu przed skanowaniem przedmiotów.")
        instruction += " W zestawach potwierdzaj każdy element osobno."
        _text(c, instruction, 214 * mm, size=8)
        for index, asset in enumerate(rows):
            top = (204 - index * 31) * mm
            _text(c, asset.name, top, bold=True, max_height=10)
            _draw_symbol(c, asset.barcode, top - 23 * mm)
            _text(c, "Kod środka: " + asset.barcode, top - 24 * mm, size=9)
            c.setStrokeColorRGB(.8, .8, .8)
            c.line(13 * mm, top - 29 * mm, 197 * mm, top - 29 * mm)
        _text(c, f"Drukuj w skali 100% | Środki: {len(assets)} | Strona {page_number} / {len(pages)}", 10 * mm, size=8)
        c.showPage()
    c.save()
    buffer.seek(0)
    return buffer

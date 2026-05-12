"""
PDF label generator for thermal transfer printers.
Label size: 50mm × 30mm. One page per asset. Code128 barcode (vector).
"""
import io

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

PAGE_W = 50 * mm
PAGE_H = 30 * mm
MARGIN = 1.5 * mm

_MAX_NAME_CHARS = 34
_BARCODE_MARGIN_X = 3.5 * mm


def generate_labels_pdf(assets, org_short_name: str) -> io.BytesIO:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H))
    for asset in assets:
        _draw_label(c, asset, org_short_name)
        c.showPage()
    c.save()
    buffer.seek(0)
    return buffer


def _draw_label(c, asset, org_short_name: str) -> None:
    _draw_org_name(c, org_short_name)
    _draw_barcode(c, asset.barcode)
    _draw_barcode_text(c, asset.barcode)
    _draw_asset_name(c, asset.name)
    _draw_inventory_number(c, asset.inventory_number)


def _draw_org_name(c, org_short_name: str) -> None:
    c.setFont("Helvetica", 4)
    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.drawCentredString(PAGE_W / 2, PAGE_H - MARGIN - 0.5 * mm, org_short_name)
    c.setFillColorRGB(0, 0, 0)


def _draw_barcode(c, barcode_value: str) -> None:
    barcode = Code128(
        barcode_value,
        barWidth=0.4 * mm,
        barHeight=17 * mm,
        humanReadable=False,
        quiet=True,
    )
    max_w = PAGE_W - 2 * _BARCODE_MARGIN_X
    y = 9 * mm
    if barcode.width > max_w:
        scale_x = max_w / barcode.width
        c.saveState()
        c.transform(scale_x, 0, 0, 1, _BARCODE_MARGIN_X, y)
        barcode.drawOn(c, 0, 0)
        c.restoreState()
    else:
        barcode.drawOn(c, (PAGE_W - barcode.width) / 2, y)


def _draw_barcode_text(c, barcode_value: str) -> None:
    c.setFont("Courier", 6)
    c.drawCentredString(PAGE_W / 2, 7 * mm, barcode_value)


def _draw_asset_name(c, name: str) -> None:
    display = name[:_MAX_NAME_CHARS] + "…" if len(name) > _MAX_NAME_CHARS else name
    c.setFont("Helvetica", 6)
    c.drawCentredString(PAGE_W / 2, 4 * mm, display)


def _draw_inventory_number(c, inventory_number: str) -> None:
    c.setFont("Helvetica", 5)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawCentredString(PAGE_W / 2, MARGIN, f"Nr inw.: {inventory_number}")
    c.setFillColorRGB(0, 0, 0)

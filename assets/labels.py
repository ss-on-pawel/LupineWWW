"""
PDF label generator for thermal transfer printers.
Label size: 50mm × 30mm. One page per asset. Code128 barcode (vector).
"""
import io

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from .documents import _BODY_FONT, _BODY_FONT_BOLD

PAGE_W = 50 * mm
PAGE_H = 30 * mm
MARGIN = 1.5 * mm
MARGIN_Y = 0.75 * mm

_MAX_NAME_CHARS = 34
_BARCODE_MARGIN_X = 1.75 * mm


def generate_labels_pdf(assets, org_short_name: str, *, layout: str = "standard") -> io.BytesIO:
    if layout not in {"standard", "50x35", "50x30"}:
        raise ValueError("Unknown asset label layout")
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_W, 35 * mm if layout == "50x35" else PAGE_H))
    for asset in assets:
        if layout in {"50x35", "50x30"}:
            _draw_compact_label(c, asset, org_short_name, height_mm=35 if layout == "50x35" else 30)
        else:
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
    c.setFont(_BODY_FONT_BOLD, 8)
    c.setFillColorRGB(0, 0, 0)
    c.drawCentredString(PAGE_W / 2, PAGE_H - MARGIN_Y - 1.5 * mm, _fit_text(org_short_name, _BODY_FONT_BOLD, 8))
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
    y = 10 * mm
    if barcode.width > max_w:
        scale_x = max_w / barcode.width
        c.saveState()
        c.transform(scale_x, 0, 0, 1, _BARCODE_MARGIN_X, y)
        barcode.drawOn(c, 0, 0)
        c.restoreState()
    else:
        barcode.drawOn(c, (PAGE_W - barcode.width) / 2, y)


def _draw_barcode_text(c, barcode_value: str) -> None:
    c.setFont("Courier-Bold", 10)
    c.drawCentredString(PAGE_W / 2, 7.25 * mm, _fit_text(barcode_value, "Courier-Bold", 10))


def _draw_asset_name(c, name: str) -> None:
    display = name[:_MAX_NAME_CHARS] + "…" if len(name) > _MAX_NAME_CHARS else name
    c.setFont(_BODY_FONT_BOLD, 9)
    c.drawCentredString(PAGE_W / 2, 4.2 * mm, _fit_text(display, _BODY_FONT_BOLD, 9))


def _draw_inventory_number(c, inventory_number: str) -> None:
    c.setFont(_BODY_FONT, 10)
    c.setFillColorRGB(0, 0, 0)
    c.drawCentredString(PAGE_W / 2, MARGIN_Y, _fit_text(f"Nr inw.: {inventory_number}", _BODY_FONT, 10))
    c.setFillColorRGB(0, 0, 0)


def _fit_text(text: str, font: str, size: float) -> str:
    """Keep enlarged text inside the label without reducing its font size."""
    max_width = PAGE_W - 2 * MARGIN
    if stringWidth(text, font, size) <= max_width:
        return text
    while text and stringWidth(text + "…", font, size) > max_width:
        text = text[:-1]
    return text + "…"

def _draw_compact_label(c, asset, organization_name: str, *, height_mm: int) -> None:
    """Compact full-width label: organization, barcode, code and asset name."""
    width, height = 50 * mm, height_mm * mm
    margin = 1.5 * mm
    available = width - 2 * margin
    c.setFillColorRGB(0, 0, 0)

    # Keep the complete organization name on one line, including Polish glyphs.
    title_size = 7.5
    title_width = stringWidth(organization_name, _BODY_FONT_BOLD, title_size)
    if title_width > available:
        title_size *= available / title_width
    c.setFont(_BODY_FONT_BOLD, title_size)
    c.drawCentredString(width / 2, height - 3 * mm, organization_name)

    barcode = Code128(asset.barcode, barWidth=0.4 * mm, barHeight=(height_mm - 16.5) * mm,
                      humanReadable=False, quiet=True, lquiet=4 * mm, rquiet=4 * mm)
    # Scale bars and their 10-module quiet zones together; never crop the symbol.
    c.saveState()
    c.translate(margin, 11.2 * mm)
    c.scale(available / barcode.width, 1)
    barcode.drawOn(c, 0, 0)
    c.restoreState()

    code_size = 12
    code_width = stringWidth(asset.barcode, _BODY_FONT_BOLD, code_size)
    if code_width > available:
        code_size *= available / code_width
    c.setFont(_BODY_FONT_BOLD, code_size)
    c.drawCentredString(width / 2, 7.2 * mm, asset.barcode)

    # Two lines for the name when no inventory number was supplied.
    name_lines = _wrap_label_name(asset.name, available, 8)
    c.setFont(_BODY_FONT_BOLD, 8)
    c.drawString(margin, 3.8 * mm, name_lines[0])
    if asset.inventory_number:
        c.setFont(_BODY_FONT, 6)
        c.drawString(margin, 1.1 * mm, _fit_text(f"Nr inw.: {asset.inventory_number}", _BODY_FONT, 6))
    elif len(name_lines) > 1:
        c.drawString(margin, 0.9 * mm, name_lines[1])


def _wrap_label_name(name: str, width: float, size: float) -> list[str]:
    remaining = " ".join(name.split())
    lines = []
    for line_number in range(2):
        if stringWidth(remaining, _BODY_FONT_BOLD, size) <= width:
            lines.append(remaining)
            break
        end = len(remaining)
        suffix = "…" if line_number == 1 else ""
        while end and stringWidth(remaining[:end] + suffix, _BODY_FONT_BOLD, size) > width:
            end -= 1
        if line_number == 0:
            space = remaining.rfind(" ", 0, end + 1)
            if space > 0:
                end = space
        lines.append(remaining[:end].rstrip() + suffix)
        remaining = remaining[end:].lstrip()
    return lines or [""]

import io
import re
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from assets.labels import generate_labels_pdf, _wrap_label_name
from assets.documents import _BODY_FONT_BOLD
from reportlab.pdfbase.pdfmetrics import stringWidth
from assets.models import Asset
from locations.models import Location
from users.models import User


class AssetLabelLayoutTests(SimpleTestCase):
    asset = SimpleNamespace(name="Szafa do biurka", barcode="100407", inventory_number="")

    def page_size(self, layout):
        data = generate_labels_pdf([self.asset], "Biblioteka Publiczna Gminy Grodzisk Mazowiecki", layout=layout).getvalue()
        match = re.search(rb'/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)', data)
        self.assertIsNotNone(match)
        return tuple(float(x) / mm for x in match.groups())

    def test_library_media_is_exactly_50x35(self):
        width, height = self.page_size("50x35")
        self.assertAlmostEqual(width, 50, places=3)
        self.assertAlmostEqual(height, 35, places=3)

    def test_compact_library_media_is_exactly_50x30(self):
        width, height = self.page_size("50x30")
        self.assertAlmostEqual(width, 50, places=3)
        self.assertAlmostEqual(height, 30, places=3)

    def test_compact_50x30_preserves_content_and_barcode_quiet_zones(self):
        from reportlab.graphics.barcode.code128 import Code128
        title = "Biblioteka Publiczna Gminy Grodzisk Mazowiecki"
        asset = SimpleNamespace(name="Szafa do biurka", barcode="100407", inventory_number="100407")
        with patch("assets.labels.Code128", wraps=Code128) as barcode, \
             patch.object(canvas.Canvas, "drawCentredString", autospec=True) as centred, \
             patch.object(canvas.Canvas, "drawString", autospec=True) as left:
            generate_labels_pdf([asset], title, layout="50x30")
        self.assertAlmostEqual(barcode.call_args.kwargs["barHeight"] / mm, 13.5)
        self.assertTrue(barcode.call_args.kwargs["quiet"])
        self.assertEqual(barcode.call_args.kwargs["lquiet"], 4 * mm)
        self.assertEqual(barcode.call_args.kwargs["rquiet"], 4 * mm)
        self.assertIn(title, [c.args[3] for c in centred.call_args_list])
        self.assertIn("100407", [c.args[3] for c in centred.call_args_list])
        self.assertIn("Szafa do biurka", [c.args[3] for c in left.call_args_list])
        self.assertIn("Nr inw.: 100407", [c.args[3] for c in left.call_args_list])

    def test_default_media_remains_50x30(self):
        width, height = self.page_size("standard")
        self.assertAlmostEqual(width, 50, places=3)
        self.assertAlmostEqual(height, 30, places=3)

    def test_library_omits_blank_inventory_number(self):
        with patch.object(canvas.Canvas, "drawString", autospec=True) as draw:
            generate_labels_pdf([self.asset], "Biblioteka", layout="50x35")
        texts = [call.args[3] for call in draw.call_args_list]
        self.assertIn("Szafa do biurka", texts)
        self.assertFalse(any(text.startswith("Nr inw.") for text in texts))

    def test_full_organization_and_alphanumeric_code_are_not_truncated(self):
        title = "Biblioteka Publiczna Gminy Grodzisk Mazowiecki"
        asset = SimpleNamespace(name="Stanowisko biblioteczne", barcode="200200/A", inventory_number="")
        with patch.object(canvas.Canvas, "drawCentredString", autospec=True) as draw:
            generate_labels_pdf([asset], title, layout="50x35")
        texts = [call.args[3] for call in draw.call_args_list]
        self.assertIn(title, texts)
        self.assertIn("200200/A", texts)

    def test_long_polish_name_fits_in_two_lines(self):
        lines = _wrap_label_name("Zestaw komputerowy z wyposażeniem i oprogramowaniem do obsługi wypożyczalni bibliotecznej", 47 * mm, 8)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        for line in lines:
            self.assertLessEqual(stringWidth(line, _BODY_FONT_BOLD, 8), 47 * mm)


class ClientLabelSettingsTests(TestCase):
    @override_settings(ASSET_LABEL_LAYOUT="50x35", ASSET_LABEL_ORGANIZATION_NAME="Biblioteka Publiczna Gminy Grodzisk Mazowiecki")
    def test_endpoint_uses_client_layout_and_name(self):
        user = User.objects.create_superuser(username="label-profile-admin")
        location = Location.objects.create(name="Etykiety")
        asset = Asset.objects.create(name="Szafa do biurka", barcode="100407", location_fk=location)
        self.client.force_login(user)
        with patch("assets.labels.generate_labels_pdf", return_value=io.BytesIO(b"%PDF-test")) as generate:
            response = self.client.get(reverse("assets:labels-pdf"), {"ids": str(asset.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(generate.call_args.args[1], "Biblioteka Publiczna Gminy Grodzisk Mazowiecki")
        self.assertEqual(generate.call_args.kwargs["layout"], "50x35")

from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .models import Asset


class AssetListApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for index in range(1, 61):
            Asset.objects.create(
                name=f"Asset {index:03d}",
                inventory_number=f"INV-{index:03d}",
                status=Asset.Status.IN_USE if index % 2 else Asset.Status.IN_STOCK,
                location="HQ" if index <= 30 else "Branch",
                category="IT" if index % 3 else "Furniture",
                purchase_value=Decimal(index * 100),
            )

        cls.target = Asset.objects.create(
            name="Laptop Executive",
            inventory_number="VIP-001",
            status=Asset.Status.RESERVED,
            location="Board Room",
            category="IT",
            purchase_value=Decimal("9999.99"),
        )

    def test_api_uses_default_pagination(self):
        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(len(payload["results"]), 50)
        self.assertEqual(payload["pagination"]["page"], 1)
        self.assertEqual(payload["pagination"]["page_size"], 50)
        self.assertEqual(payload["pagination"]["total_items"], 61)
        self.assertTrue(payload["pagination"]["has_next"])

    def test_api_filters_and_searches(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "search": "VIP",
                "status": Asset.Status.RESERVED,
                "location": "Board Room",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], self.target.id)
        self.assertEqual(payload["results"][0]["name"], "Laptop Executive")

    def test_api_supports_sorting_by_value(self):
        response = self.client.get(reverse("assets:api-list"), {"ordering": "-value", "page_size": 5})

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")
        self.assertEqual(payload["filters"]["ordering"], "-purchase_value")


class AssetListViewTests(TestCase):
    def test_list_view_exposes_filters(self):
        Asset.objects.create(
            name="Monitor",
            inventory_number="MON-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-api-url="/api/assets/"')
        self.assertContains(response, "<option value=\"Warehouse\">Warehouse</option>", html=True)


class SeedAssetsCommandTests(TestCase):
    def test_seed_assets_creates_requested_records(self):
        stdout = StringIO()

        call_command("seed_assets", "--count", "25", stdout=stdout)

        seeded_assets = Asset.objects.filter(external_id__startswith="seed_asset:")
        self.assertEqual(seeded_assets.count(), 25)
        self.assertTrue(all(asset.inventory_number.startswith("SEED-AST-") for asset in seeded_assets))
        self.assertIn("Utworzono: 25", stdout.getvalue())

    def test_seed_assets_clear_removes_only_seeded_records(self):
        Asset.objects.create(
            name="Existing Asset",
            inventory_number="INV-EXIST-001",
            status=Asset.Status.IN_STOCK,
            location="HQ",
            category="IT",
        )
        call_command("seed_assets", "--count", "10")

        stdout = StringIO()
        call_command("seed_assets", "--count", "0", "--clear", stdout=stdout)

        self.assertTrue(Asset.objects.filter(inventory_number="INV-EXIST-001").exists())
        self.assertFalse(Asset.objects.filter(external_id__startswith="seed_asset:").exists())
        self.assertIn("Usunieto 10", stdout.getvalue())

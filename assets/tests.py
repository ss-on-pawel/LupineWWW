from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from users.models import User
from locations.models import Location

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
            purchase_date=date(2024, 6, 15),
        )

        cls.date_outside = Asset.objects.create(
            name="Archive Router",
            inventory_number="ARC-001",
            status=Asset.Status.RESERVED,
            location="Archive",
            category="IT",
            purchase_value=Decimal("2500"),
            purchase_date=date(2023, 3, 10),
        )

    def test_api_uses_default_pagination(self):
        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(len(payload["results"]), 50)
        self.assertEqual(payload["pagination"]["page"], 1)
        self.assertEqual(payload["pagination"]["page_size"], 50)
        self.assertEqual(payload["pagination"]["total_items"], 62)
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

    def test_api_applies_backend_filters_from_query_params(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "filter__status__equals": Asset.Status.RESERVED,
                "filter__purchase_value__gt": "5000",
                "filter__name__contains": "Laptop",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_supports_enum_in_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__status__in": ",".join([Asset.Status.RESERVED, Asset.Status.IN_USE])},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 32)

    def test_api_supports_number_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_value__between": "9900,10000"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_supports_date_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_date__between": "2024-01-01,2024-12-31"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_ignores_incomplete_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_value__between": "1000,"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 62)

    def test_api_ignores_invalid_between_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__purchase_date__between": "2024-12-31,2024-01-01"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 62)

    def test_api_combines_between_with_other_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "filter__purchase_date__between": "2024-01-01,2024-12-31",
                "filter__status__equals": Asset.Status.RESERVED,
                "filter__name__contains": "Laptop",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_exposes_extended_system_columns(self):
        user = User.objects.create_user(username="operator", first_name="Jan", last_name="Kowalski")
        asset = Asset.objects.create(
            name="Laptop Test",
            inventory_number="EXT-001",
            asset_type=Asset.AssetType.IT_EQUIPMENT,
            manufacturer="Dell",
            model="Latitude",
            serial_number="SN-123",
            barcode="5901234567890",
            department="IT",
            organizational_unit="Centrala",
            room="201A",
            responsible_person=user,
            current_user=user,
            technical_condition=Asset.TechnicalCondition.VERY_GOOD,
            invoice_number="FV/2026/001",
            external_id="ERP-001",
            cost_center="MPK-01",
            is_active=True,
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["asset_type_display"], "Sprzęt IT")
        self.assertEqual(row["manufacturer"], "Dell")
        self.assertEqual(row["serial_number"], "SN-123")
        self.assertEqual(row["responsible_person"], "Jan Kowalski")
        self.assertEqual(row["current_user"], "Jan Kowalski")
        self.assertEqual(row["technical_condition_display"], "Bardzo dobry")
        self.assertEqual(row["invoice_number"], "FV/2026/001")
        self.assertEqual(row["external_id"], "ERP-001")
        self.assertEqual(row["cost_center"], "MPK-01")
        self.assertEqual(row["is_active_display"], "Tak")


class AssetBulkMoveApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.target_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.inactive_location = Location.objects.create(name="Archiwum", parent=cls.root_location, is_active=False)
        cls.asset_one = Asset.objects.create(
            name="Bulk Asset 1",
            inventory_number="BULK-001",
            status=Asset.Status.IN_STOCK,
            location="HQ",
            category="IT",
        )
        cls.asset_two = Asset.objects.create(
            name="Bulk Asset 2",
            inventory_number="BULK-002",
            status=Asset.Status.IN_USE,
            location="Branch",
            category="IT",
        )

    def test_bulk_move_updates_assets(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_one.id, self.asset_two.id],
                "target_location_id": self.target_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "success": True,
                "updated_count": 2,
                "target_location_id": self.target_location.id,
                "target_location_path": self.target_location.path,
            },
        )
        self.asset_one.refresh_from_db()
        self.asset_two.refresh_from_db()
        self.assertEqual(self.asset_one.location, self.target_location.path)
        self.assertEqual(self.asset_two.location, self.target_location.path)
        self.assertEqual(self.asset_one.location_fk, self.target_location)
        self.assertEqual(self.asset_two.location_fk, self.target_location)

    def test_bulk_move_rejects_empty_asset_ids(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [], "target_location_id": self.target_location.id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["success"], False)

    def test_bulk_move_rejects_invalid_target_location_id(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [self.asset_one.id], "target_location_id": ""},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["success"], False)

    def test_bulk_move_rejects_missing_or_inactive_target_location_id(self):
        missing_response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [self.asset_one.id]},
            content_type="application/json",
        )
        inactive_response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={"asset_ids": [self.asset_one.id], "target_location_id": self.inactive_location.id},
            content_type="application/json",
        )

        self.assertEqual(missing_response.status_code, 400)
        self.assertEqual(missing_response.json()["success"], False)
        self.assertEqual(inactive_response.status_code, 400)
        self.assertEqual(inactive_response.json()["success"], False)

    def test_bulk_move_rejects_get(self):
        response = self.client.get(reverse("assets:api-bulk-move"))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["success"], False)


class AssetListViewTests(TestCase):
    def test_list_view_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_list_view_exposes_filters_for_authenticated_user(self):
        Asset.objects.create(
            name="Monitor",
            inventory_number="MON-001",
            status=Asset.Status.IN_STOCK,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_user(username="viewer", password="test-pass-123")
        self.client.force_login(user)

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


class BackfillAssetLocationFkCommandTests(TestCase):
    def test_dry_run_reports_matches_without_saving(self):
        root = Location.objects.create(name="Warszawa")
        target = Location.objects.create(name="Magazyn A", parent=root)
        matching_asset = Asset.objects.create(
            name="Laptop Match",
            inventory_number="BF-001",
            status=Asset.Status.IN_STOCK,
            location=target.path,
            category="IT",
        )
        Asset.objects.create(
            name="Laptop Empty",
            inventory_number="BF-002",
            status=Asset.Status.IN_STOCK,
            location="",
            category="IT",
        )
        Asset.objects.create(
            name="Laptop Miss",
            inventory_number="BF-003",
            status=Asset.Status.IN_STOCK,
            location="Nieistniejaca / Sciezka",
            category="IT",
        )

        stdout = StringIO()
        call_command("backfill_asset_location_fk", "--dry-run", stdout=stdout)

        matching_asset.refresh_from_db()
        self.assertIsNone(matching_asset.location_fk)
        self.assertIn("Pewne dopasowania: 1", stdout.getvalue())
        self.assertIn("Puste location: 1", stdout.getvalue())
        self.assertIn("Bez dopasowania: 1", stdout.getvalue())
        self.assertIn("Pozostaje bez location_fk: 2", stdout.getvalue())

    def test_command_backfills_only_exact_path_matches(self):
        root = Location.objects.create(name="Krakow")
        target = Location.objects.create(name="Biuro", parent=root)
        matching_asset = Asset.objects.create(
            name="Laptop Match 2",
            inventory_number="BF-010",
            status=Asset.Status.IN_STOCK,
            location=target.path,
            category="IT",
        )
        unmatched_asset = Asset.objects.create(
            name="Laptop Miss 2",
            inventory_number="BF-011",
            status=Asset.Status.IN_STOCK,
            location="Krakow / Nieistniejace",
            category="IT",
        )

        stdout = StringIO()
        call_command("backfill_asset_location_fk", stdout=stdout)

        matching_asset.refresh_from_db()
        unmatched_asset.refresh_from_db()
        self.assertEqual(matching_asset.location_fk_id, target.id)
        self.assertIsNone(unmatched_asset.location_fk)
        self.assertIn("Pewne dopasowania: 1", stdout.getvalue())
        self.assertIn("Bez dopasowania: 1", stdout.getvalue())
        self.assertIn("Pozostaje bez location_fk: 1", stdout.getvalue())

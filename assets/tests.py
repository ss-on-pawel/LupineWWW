import csv
import json
import importlib

from datetime import date, datetime
from decimal import Decimal
from io import StringIO

from django.apps import apps as django_apps
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.contrib.messages import get_messages
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from users.models import User
from locations.models import Location
from inventory.models import InventorySession, InventorySnapshotItem

from .forms import AssetForm
from .filters import get_asset_filter_ui_schema
from .models import Asset, AssetBarcodeSequence, AssetChangeRequest, AssetHistoryEntry, AssetTypeDictionary
from .services import (
    approve_asset_change_request,
    deserialize_asset_payload_for_form,
    get_asset_withdraw_capabilities,
    generate_unique_asset_barcode,
    reject_asset_change_request,
    serialize_asset_form_payload,
    user_requires_asset_change_approval,
)
from .views import AssetChangeRequestListView, _user_can_review_asset_changes


backfill_asset_type_ref = importlib.import_module(
    "assets.migrations.0010_asset_asset_type_ref"
).backfill_asset_type_ref
simplify_asset_statuses = importlib.import_module(
    "assets.migrations.0018_simplify_asset_status"
).simplify_asset_statuses


class AssetTypeDictionaryModelTests(TestCase):
    def test_default_asset_types_exist_after_migrations(self):
        expected = {
            "fixed": ("Środek trwały", False, 10, True, "ST"),
            "low_value": ("Wyposażenie / niskocenne", False, 20, True, "WN"),
            "intangible": ("WNiP", False, 30, True, "WP"),
            "quantity": ("Ilościówka", True, 40, True, "IL"),
            "other": ("Inne", False, 50, True, "IN"),
        }

        rows = {
            item.code: item
            for item in AssetTypeDictionary.objects.filter(code__in=expected)
        }

        self.assertEqual(set(rows), set(expected))
        for code, (name, is_quantity_based, sort_order, is_system, barcode_prefix) in expected.items():
            with self.subTest(code=code):
                self.assertEqual(rows[code].name, name)
                self.assertEqual(rows[code].is_quantity_based, is_quantity_based)
                self.assertTrue(rows[code].is_active)
                self.assertEqual(rows[code].sort_order, sort_order)
                self.assertEqual(rows[code].is_system, is_system)
                self.assertEqual(rows[code].barcode_prefix, barcode_prefix)

    def test_code_is_unique(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AssetTypeDictionary.objects.create(name="Duplicate fixed", code="fixed")

    def test_quantity_is_quantity_based(self):
        self.assertTrue(AssetTypeDictionary.objects.get(code="quantity").is_quantity_based)

    def test_non_quantity_defaults_are_not_quantity_based(self):
        non_quantity_codes = ["fixed", "low_value", "intangible", "other"]

        self.assertFalse(
            AssetTypeDictionary.objects
            .filter(code__in=non_quantity_codes, is_quantity_based=True)
            .exists()
        )

    def test_str_returns_name(self):
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        self.assertEqual(str(asset_type), asset_type.name)

    def test_existing_asset_type_choices_remain_available(self):
        self.assertEqual(Asset.AssetType.FIXED, "fixed")
        self.assertIn(("fixed", "Środek trwały"), Asset.AssetType.choices)
        self.assertIn(("quantity", "Ilościówka"), Asset.AssetType.choices)


class AssetTypeRefBridgeMigrationTests(TestCase):
    def test_backfill_assigns_matching_asset_type_ref(self):
        asset = Asset.objects.create(
            name="Fixed bridge asset",
            inventory_number="BRIDGE-FIXED-001",
            asset_type=Asset.AssetType.FIXED,
        )
        Asset.objects.filter(pk=asset.pk).update(asset_type_ref=None)

        backfill_asset_type_ref(django_apps, None)

        asset.refresh_from_db()
        self.assertEqual(asset.asset_type_ref.code, "fixed")

    def test_backfill_assigns_other_when_code_has_no_match(self):
        asset = Asset.objects.create(
            name="Legacy bridge asset",
            inventory_number="BRIDGE-LEGACY-001",
            asset_type=Asset.AssetType.FIXED,
        )
        Asset.objects.filter(pk=asset.pk).update(asset_type="legacy_unknown", asset_type_ref=None)

        backfill_asset_type_ref(django_apps, None)

        asset.refresh_from_db()
        self.assertEqual(asset.asset_type_ref.code, "other")

    def test_backfill_does_not_overwrite_existing_asset_type_ref(self):
        low_value_type = AssetTypeDictionary.objects.get(code="low_value")
        asset = Asset.objects.create(
            name="Preset bridge asset",
            inventory_number="BRIDGE-PRESET-001",
            asset_type=Asset.AssetType.FIXED,
        )
        Asset.objects.filter(pk=asset.pk).update(asset_type_ref=low_value_type)

        backfill_asset_type_ref(django_apps, None)

        asset.refresh_from_db()
        self.assertEqual(asset.asset_type_ref.code, "low_value")

    def test_asset_type_ref_does_not_change_existing_choices(self):
        self.assertEqual(
            list(Asset.AssetType.choices),
            [
                ("fixed", "Środek trwały"),
                ("low_value", "Wyposażenie / niskocenne"),
                ("intangible", "WNiP"),
                ("quantity", "Ilościówka"),
                ("other", "Inne"),
            ],
        )


class AssetTypeFieldSyncTests(TestCase):
    def test_save_with_asset_type_sets_asset_type_ref(self):
        asset = Asset.objects.create(
            name="Sync fixed asset",
            inventory_number="SYNC-FIXED-001",
            asset_type=Asset.AssetType.FIXED,
        )

        self.assertEqual(asset.asset_type_ref.code, "fixed")

    def test_save_with_asset_type_ref_sets_asset_type_code(self):
        fixed_type = AssetTypeDictionary.objects.get(code="fixed")

        asset = Asset.objects.create(
            name="Sync ref asset",
            inventory_number="SYNC-REF-001",
            asset_type="",
            asset_type_ref=fixed_type,
        )

        self.assertEqual(asset.asset_type, "fixed")

    def test_asset_type_wins_when_code_and_ref_conflict(self):
        fixed_type = AssetTypeDictionary.objects.get(code="fixed")

        asset = Asset.objects.create(
            name="Sync conflict asset",
            inventory_number="SYNC-CONFLICT-001",
            asset_type=Asset.AssetType.LOW_VALUE,
            asset_type_ref=fixed_type,
        )

        self.assertEqual(asset.asset_type, "low_value")
        self.assertEqual(asset.asset_type_ref.code, "low_value")

    def test_unknown_asset_type_falls_back_to_other(self):
        asset = Asset.objects.create(
            name="Sync unknown asset",
            inventory_number="SYNC-UNKNOWN-001",
            asset_type="unknown",
        )

        self.assertEqual(asset.asset_type, "other")
        self.assertEqual(asset.asset_type_ref.code, "other")

    def test_save_with_update_fields_persists_synced_ref(self):
        asset = Asset.objects.create(
            name="Sync update fields asset",
            inventory_number="SYNC-UPDATE-FIELDS-001",
            asset_type=Asset.AssetType.FIXED,
        )

        asset.asset_type = Asset.AssetType.LOW_VALUE
        asset.save(update_fields=["asset_type"])

        asset.refresh_from_db()
        self.assertEqual(asset.asset_type, "low_value")
        self.assertEqual(asset.asset_type_ref.code, "low_value")


class AssetStatusModelTests(TestCase):
    def test_default_status_is_active(self):
        asset = Asset.objects.create(
            name="Status default asset",
            inventory_number="STATUS-DEFAULT-001",
        )

        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertTrue(asset.is_active)

    def test_liquidated_status_marks_asset_inactive(self):
        asset = Asset.objects.create(
            name="Status liquidated asset",
            inventory_number="STATUS-LIQUIDATED-001",
            status=Asset.Status.ACTIVE,
        )

        asset.status = Asset.Status.LIQUIDATED
        asset.save(update_fields=["status"])
        asset.refresh_from_db()

        self.assertFalse(asset.is_active)

    def test_active_and_inactive_statuses_mark_asset_active(self):
        asset = Asset.objects.create(
            name="Status inactive asset",
            inventory_number="STATUS-INACTIVE-001",
            status=Asset.Status.LIQUIDATED,
        )

        asset.status = Asset.Status.INACTIVE
        asset.save(update_fields=["status"])
        asset.refresh_from_db()

        self.assertTrue(asset.is_active)


class AssetStatusMigrationTests(TestCase):
    def test_simplify_asset_statuses_maps_legacy_values_and_snapshots(self):
        user = User.objects.create_user(username="status-migration-user", password="test-pass-123")
        location = Location.objects.create(name="Status Migration Location")
        asset = Asset.objects.create(
            name="Status migration asset",
            inventory_number="STATUS-MIGRATION-001",
            status=Asset.Status.ACTIVE,
            location_fk=location,
        )
        Asset.objects.filter(pk=asset.pk).update(status="sold", is_active=True)
        session = InventorySession.objects.create(number="SM0001", created_by=user)
        snapshot = InventorySnapshotItem.objects.create(
            session=session,
            asset=asset,
            asset_id_snapshot=asset.pk,
            inventory_number=asset.inventory_number,
            name=asset.name,
            location_fk_id_snapshot=location.pk,
            status_snapshot="in_service",
        )

        simplify_asset_statuses(django_apps, None)

        asset.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
        self.assertFalse(asset.is_active)
        self.assertEqual(snapshot.status_snapshot, Asset.Status.INACTIVE)


class AssetRecordQuantityModelTests(TestCase):
    def test_record_quantity_defaults_to_one(self):
        asset = Asset.objects.create(
            name="Record quantity default asset",
            inventory_number="RQ-DEFAULT-001",
        )

        self.assertEqual(asset.record_quantity, 1)

    def test_record_quantity_can_store_non_negative_value(self):
        asset = Asset.objects.create(
            name="Record quantity custom asset",
            inventory_number="RQ-CUSTOM-001",
            record_quantity=0,
        )

        self.assertEqual(asset.record_quantity, 0)

    def test_current_quantity_defaults_to_one(self):
        asset = Asset.objects.create(
            name="Current quantity default asset",
            inventory_number="CQ-DEFAULT-001",
        )

        self.assertEqual(asset.current_quantity, 1)

    def test_current_quantity_can_store_zero(self):
        asset = Asset.objects.create(
            name="Current quantity zero asset",
            inventory_number="CQ-ZERO-001",
            current_quantity=0,
        )

        self.assertEqual(asset.current_quantity, 0)

    def test_current_quantity_is_not_calculated_from_record_quantity(self):
        asset = Asset.objects.create(
            name="Current quantity independent record asset",
            inventory_number="CQ-INDEPENDENT-RECORD-001",
            record_quantity=7,
            current_quantity=2,
            last_inventory_quantity=None,
        )

        self.assertEqual(asset.current_quantity, 2)

    def test_changing_last_inventory_quantity_does_not_change_current_quantity(self):
        asset = Asset.objects.create(
            name="Current quantity independent inventory asset",
            inventory_number="CQ-INDEPENDENT-INVENTORY-001",
            record_quantity=7,
            current_quantity=4,
            last_inventory_quantity=3,
        )

        asset.last_inventory_quantity = 0
        asset.save(update_fields=["last_inventory_quantity", "updated_at"])
        asset.refresh_from_db()

        self.assertEqual(asset.current_quantity, 4)


class AssetWithdrawCapabilitiesTests(TestCase):
    def _create_asset(self, inventory_number, **overrides):
        defaults = {
            "name": "Withdraw capabilities asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "record_quantity": 1,
            "current_quantity": 1,
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def test_regular_asset_with_quantity_one_can_only_be_fully_withdrawn(self):
        asset = self._create_asset("WITHDRAW-CAP-FIXED-001")

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertTrue(capabilities["can_full_withdraw"])
        self.assertFalse(capabilities["can_partial_withdraw"])
        self.assertFalse(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 1)

    def test_quantity_asset_with_quantity_one_can_only_be_fully_withdrawn(self):
        asset = self._create_asset(
            "WITHDRAW-CAP-QTY-ONE-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=1,
        )

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertTrue(capabilities["can_full_withdraw"])
        self.assertFalse(capabilities["can_partial_withdraw"])
        self.assertTrue(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 1)

    def test_quantity_asset_with_quantity_above_one_can_be_partially_withdrawn(self):
        asset = self._create_asset(
            "WITHDRAW-CAP-QTY-MANY-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=3,
            current_quantity=3,
        )

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertTrue(capabilities["can_full_withdraw"])
        self.assertTrue(capabilities["can_partial_withdraw"])
        self.assertTrue(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 3)

    def test_quantity_fallback_works_without_asset_type_ref(self):
        asset = self._create_asset(
            "WITHDRAW-CAP-QTY-FALLBACK-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=4,
            current_quantity=4,
        )
        Asset.objects.filter(pk=asset.pk).update(asset_type_ref=None)
        asset.refresh_from_db()

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertTrue(capabilities["can_full_withdraw"])
        self.assertTrue(capabilities["can_partial_withdraw"])
        self.assertTrue(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 4)

    def test_unknown_legacy_asset_type_is_not_quantity_based(self):
        asset = self._create_asset("WITHDRAW-CAP-LEGACY-001", record_quantity=5, current_quantity=5)
        Asset.objects.filter(pk=asset.pk).update(asset_type="legacy_unknown", asset_type_ref=None)
        asset.refresh_from_db()

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertTrue(capabilities["can_full_withdraw"])
        self.assertFalse(capabilities["can_partial_withdraw"])
        self.assertFalse(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 5)

    def test_inactive_asset_cannot_be_withdrawn(self):
        asset = self._create_asset(
            "WITHDRAW-CAP-INACTIVE-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=5,
            current_quantity=5,
            status=Asset.Status.LIQUIDATED,
            is_active=False,
        )

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertFalse(capabilities["can_full_withdraw"])
        self.assertFalse(capabilities["can_partial_withdraw"])
        self.assertTrue(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 5)

    def test_zero_current_quantity_is_preserved(self):
        asset = self._create_asset(
            "WITHDRAW-CAP-ZERO-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=5,
            current_quantity=0,
            last_inventory_quantity=0,
        )

        capabilities = get_asset_withdraw_capabilities(asset)

        self.assertTrue(capabilities["can_full_withdraw"])
        self.assertFalse(capabilities["can_partial_withdraw"])
        self.assertTrue(capabilities["is_quantity_based"])
        self.assertEqual(capabilities["current_quantity"], 0)


class AssetLocationSyncModelTests(TestCase):
    def test_save_syncs_location_cache_from_location_fk_path(self):
        root = Location.objects.create(name="Sync Root")
        location = Location.objects.create(name="Sync Room", parent=root)
        asset = Asset.objects.create(
            name="Location sync asset",
            inventory_number="LOC-SYNC-001",
            location="Stale legacy value",
            location_fk=location,
        )

        self.assertEqual(asset.location, location.path)

    def test_save_without_location_fk_keeps_legacy_location_for_old_records(self):
        asset = Asset.objects.create(
            name="Legacy location asset",
            inventory_number="LOC-LEGACY-001",
            location="Legacy only",
            location_fk=None,
        )

        self.assertEqual(asset.location, "Legacy only")
        self.assertIsNone(asset.location_fk)


class AssetBarcodeGeneratorTests(TestCase):
    def _generated_at(self, year):
        return timezone.make_aware(datetime(year, 1, 15, 10, 0, 0))

    def test_generator_creates_first_barcode_for_prefix_and_year(self):
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        barcode = generate_unique_asset_barcode(
            asset_type_ref=asset_type,
            generated_at=self._generated_at(2026),
        )

        self.assertEqual(barcode, "ST260000001")
        sequence = AssetBarcodeSequence.objects.get(prefix="ST", year=2026)
        self.assertEqual(sequence.next_number, 2)

    def test_generator_increments_for_same_prefix_and_year(self):
        asset_type = AssetTypeDictionary.objects.get(code="fixed")
        generated_at = self._generated_at(2026)

        first = generate_unique_asset_barcode(asset_type_ref=asset_type, generated_at=generated_at)
        second = generate_unique_asset_barcode(asset_type_ref=asset_type, generated_at=generated_at)

        self.assertEqual(first, "ST260000001")
        self.assertEqual(second, "ST260000002")

    def test_generator_uses_separate_counter_for_other_prefix(self):
        fixed_type = AssetTypeDictionary.objects.get(code="fixed")
        low_value_type = AssetTypeDictionary.objects.get(code="low_value")
        generated_at = self._generated_at(2026)

        fixed_barcode = generate_unique_asset_barcode(asset_type_ref=fixed_type, generated_at=generated_at)
        low_value_barcode = generate_unique_asset_barcode(asset_type_ref=low_value_type, generated_at=generated_at)

        self.assertEqual(fixed_barcode, "ST260000001")
        self.assertEqual(low_value_barcode, "WN260000001")

    def test_generator_resets_counter_for_new_year(self):
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        old_year_barcode = generate_unique_asset_barcode(
            asset_type_ref=asset_type,
            generated_at=self._generated_at(2026),
        )
        new_year_barcode = generate_unique_asset_barcode(
            asset_type_ref=asset_type,
            generated_at=self._generated_at(2027),
        )

        self.assertEqual(old_year_barcode, "ST260000001")
        self.assertEqual(new_year_barcode, "ST270000001")

    def test_generator_skips_existing_asset_barcode_collision(self):
        Asset.objects.create(
            name="Existing barcode collision",
            inventory_number="COLLISION-BARCODE-001",
            barcode="ST260000001",
        )
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        barcode = generate_unique_asset_barcode(
            asset_type_ref=asset_type,
            generated_at=self._generated_at(2026),
        )

        self.assertEqual(barcode, "ST260000002")

    def test_generator_skips_inventory_number_collision(self):
        Asset.objects.create(
            name="Existing inventory collision",
            inventory_number="ST260000001",
        )
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        barcode = generate_unique_asset_barcode(
            asset_type_ref=asset_type,
            generated_at=self._generated_at(2026),
        )

        self.assertEqual(barcode, "ST260000002")

    def test_generator_skips_location_code_collision(self):
        Location.objects.create(name="Barcode collision location", code="ST260000001")
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        barcode = generate_unique_asset_barcode(
            asset_type_ref=asset_type,
            generated_at=self._generated_at(2026),
        )

        self.assertEqual(barcode, "ST260000002")

    def test_generator_rejects_asset_type_without_prefix(self):
        asset_type = AssetTypeDictionary.objects.create(
            name="No prefix type",
            code="no-prefix",
            is_active=True,
            sort_order=90,
        )

        with self.assertRaisesMessage(
            ValidationError,
            "Rodzaj środka nie ma skonfigurowanego prefixu kodu kreskowego.",
        ):
            generate_unique_asset_barcode(asset_type_ref=asset_type, generated_at=self._generated_at(2026))

    def test_generator_rejects_exhausted_sequence(self):
        asset_type = AssetTypeDictionary.objects.get(code="fixed")
        AssetBarcodeSequence.objects.create(prefix="ST", year=2026, next_number=10_000_000)

        with self.assertRaisesMessage(
            ValidationError,
            "Wyczerpano pulę kodów kreskowych dla tego prefixu i roku.",
        ):
            generate_unique_asset_barcode(asset_type_ref=asset_type, generated_at=self._generated_at(2026))


class AssetFormAssetTypeDictionaryTests(TestCase):
    def _valid_form_data(self, **overrides):
        location, _ = Location.objects.get_or_create(name="Form dictionary location")
        data = {
            "name": "Form dictionary asset",
            "inventory_number": "FORM-DICT-001",
            "asset_type": "fixed",
            "category": "",
            "manufacturer": "",
            "model": "",
            "serial_number": "",
            "barcode": "",
            "description": "",
            "purchase_date": "",
            "commissioning_date": "",
            "purchase_value": "",
            "invoice_number": "",
            "external_id": "",
            "cost_center": "",
            "organizational_unit": "",
            "department": "",
            "location_fk": str(location.id),
            "room": "",
            "responsible_person": "",
            "current_user": "",
            "current_quantity": "1",
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "last_inventory_date": "",
            "next_review_date": "",
            "warranty_until": "",
            "insurance_until": "",
            "is_active": "on",
        }
        data.update(overrides)
        return data

    def test_form_shows_active_asset_types_from_dictionary(self):
        form = AssetForm()

        choices = dict(form.fields["asset_type"].choices)
        self.assertEqual(choices["fixed"], "Środek trwały")
        self.assertEqual(choices["low_value"], "Wyposażenie / niskocenne")
        self.assertEqual(choices["quantity"], "Ilościówka")

    def test_form_does_not_show_inactive_asset_types(self):
        AssetTypeDictionary.objects.filter(code="other").update(is_active=False)

        form = AssetForm()

        choice_values = {value for value, _label in form.fields["asset_type"].choices}
        self.assertNotIn("other", choice_values)

    def test_form_requires_location_fk_and_does_not_expose_legacy_location(self):
        form = AssetForm(data=self._valid_form_data(location_fk=""))

        self.assertFalse(form.is_valid())
        self.assertIn("location_fk", form.errors)
        self.assertIn("location_fk", form.fields)
        self.assertNotIn("location", form.fields)

    def test_form_save_sets_asset_type_ref_from_selected_code(self):
        form = AssetForm(data=self._valid_form_data(asset_type="low_value"))

        self.assertTrue(form.is_valid(), form.errors)
        asset = form.save()

        self.assertEqual(asset.asset_type_ref.code, "low_value")

    def test_form_save_generates_barcode_for_new_asset_without_barcode(self):
        form = AssetForm(data=self._valid_form_data(inventory_number="FORM-GEN-001", barcode=""))

        self.assertTrue(form.is_valid(), form.errors)
        asset = form.save()

        expected_year = timezone.now().year % 100
        self.assertEqual(asset.barcode, f"ST{expected_year:02d}0000001")
        self.assertEqual(asset.asset_type_ref.code, "fixed")

    def test_form_save_generates_next_barcode_for_same_prefix_and_year(self):
        first_form = AssetForm(data=self._valid_form_data(inventory_number="FORM-GEN-SEQ-001", barcode=""))
        second_form = AssetForm(data=self._valid_form_data(inventory_number="FORM-GEN-SEQ-002", barcode=""))

        self.assertTrue(first_form.is_valid(), first_form.errors)
        self.assertTrue(second_form.is_valid(), second_form.errors)
        first_asset = first_form.save()
        second_asset = second_form.save()

        expected_year = timezone.now().year % 100
        self.assertEqual(first_asset.barcode, f"ST{expected_year:02d}0000001")
        self.assertEqual(second_asset.barcode, f"ST{expected_year:02d}0000002")

    def test_form_save_preserves_manual_barcode(self):
        form = AssetForm(data=self._valid_form_data(inventory_number="FORM-MANUAL-001", barcode="MANUAL-CODE-001"))

        self.assertTrue(form.is_valid(), form.errors)
        asset = form.save()

        self.assertEqual(asset.barcode, "MANUAL-CODE-001")
        self.assertFalse(AssetBarcodeSequence.objects.exists())

    def test_form_update_does_not_generate_new_barcode(self):
        location, _ = Location.objects.get_or_create(name="Form update location")
        asset = Asset.objects.create(
            name="Update barcode asset",
            inventory_number="FORM-UPD-BARCODE-001",
            asset_type=Asset.AssetType.FIXED,
            barcode="KEEP-ME-001",
            location_fk=location,
            location=location.path,
            status=Asset.Status.ACTIVE,
        )
        form = AssetForm(
            data=self._valid_form_data(
                name="Updated barcode asset",
                inventory_number=asset.inventory_number,
                barcode=asset.barcode,
                location_fk=str(location.id),
            ),
            instance=asset,
        )

        self.assertTrue(form.is_valid(), form.errors)
        updated_asset = form.save()

        self.assertEqual(updated_asset.barcode, "KEEP-ME-001")
        self.assertFalse(AssetBarcodeSequence.objects.exists())

    def test_form_rejects_manual_barcode_matching_inventory_number(self):
        Asset.objects.create(name="Inventory collision asset", inventory_number="MANUAL-COLLISION-INV")
        form = AssetForm(data=self._valid_form_data(inventory_number="FORM-MANUAL-INV-001", barcode="MANUAL-COLLISION-INV"))

        self.assertFalse(form.is_valid())
        self.assertIn("barcode", form.errors)

    def test_form_rejects_manual_barcode_matching_location_code(self):
        Location.objects.create(name="Manual barcode collision location", code="MANUAL-COLLISION-LOC")
        form = AssetForm(data=self._valid_form_data(inventory_number="FORM-MANUAL-LOC-001", barcode="MANUAL-COLLISION-LOC"))

        self.assertFalse(form.is_valid())
        self.assertIn("barcode", form.errors)

    def test_form_without_asset_type_and_barcode_adds_asset_type_error(self):
        form = AssetForm(data=self._valid_form_data(inventory_number="FORM-NO-TYPE-001", asset_type="", barcode=""))

        self.assertFalse(form.is_valid())
        self.assertIn("asset_type", form.errors)
        self.assertIn(
            "Wybierz rodzaj środka, aby wygenerować kod kreskowy.",
            form.errors["asset_type"],
        )

    def test_form_without_barcode_requires_asset_type_prefix(self):
        AssetTypeDictionary.objects.create(
            name="No prefix form type",
            code="no-prefix-form",
            is_active=True,
            sort_order=95,
        )
        form = AssetForm(data=self._valid_form_data(inventory_number="FORM-NO-PREFIX-001", asset_type="no-prefix-form", barcode=""))

        self.assertFalse(form.is_valid())
        self.assertIn("asset_type", form.errors)
        self.assertIn(
            "Wybrany rodzaj środka nie ma skonfigurowanego prefixu kodu kreskowego.",
            form.errors["asset_type"],
        )

    def test_form_save_keeps_asset_type_as_code(self):
        form = AssetForm(data=self._valid_form_data(asset_type="quantity"))

        self.assertTrue(form.is_valid(), form.errors)
        asset = form.save()

        self.assertEqual(asset.asset_type, "quantity")

    def test_form_accepts_custom_active_dictionary_type(self):
        AssetTypeDictionary.objects.create(
            name="Custom active type",
            code="custom-active-type",
            barcode_prefix="CT",
            is_active=True,
            sort_order=70,
        )
        form = AssetForm(data=self._valid_form_data(asset_type="custom-active-type"))

        self.assertTrue(form.is_valid(), form.errors)
        asset = form.save()

        self.assertEqual(asset.asset_type, "custom-active-type")
        self.assertEqual(asset.asset_type_ref.code, "custom-active-type")

    def test_edit_existing_asset_uses_current_asset_type(self):
        asset = Asset.objects.create(
            name="Existing form dictionary asset",
            inventory_number="FORM-DICT-EXISTING-001",
            asset_type=Asset.AssetType.INTANGIBLE,
        )

        form = AssetForm(instance=asset)

        self.assertEqual(form["asset_type"].value(), "intangible")

    def test_edit_existing_asset_falls_back_to_asset_type_ref(self):
        fixed_type = AssetTypeDictionary.objects.get(code="fixed")
        asset = Asset.objects.create(
            name="Existing form ref asset",
            inventory_number="FORM-DICT-REF-001",
            asset_type=Asset.AssetType.LOW_VALUE,
        )
        Asset.objects.filter(pk=asset.pk).update(asset_type="", asset_type_ref=fixed_type)
        asset.refresh_from_db()

        form = AssetForm(instance=asset)

        self.assertEqual(form["asset_type"].value(), "fixed")


class AssetTypeDictionarySettingsViewTests(TestCase):
    def _form_data(self, **overrides):
        data = {
            "name": "Custom type",
            "code": "custom-type",
            "barcode_prefix": "",
            "is_quantity_based": "",
            "is_active": "on",
            "sort_order": "60",
        }
        data.update(overrides)
        return data

    def _manager_user(self):
        user = User.objects.create_user(username="asset-type-manager", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        return user

    def test_staff_admin_can_access_list(self):
        user = User.objects.create_user(username="asset-type-staff", password="test-pass-123", is_staff=True)
        self.client.force_login(user)

        response = self.client.get(reverse("settings:asset-types"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rodzaje środków")

    def test_manager_can_access_list(self):
        self.client.force_login(self._manager_user())

        response = self.client.get(reverse("settings:asset-types"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dodaj rodzaj")

    def test_list_shows_barcode_prefix(self):
        self.client.force_login(self._manager_user())

        response = self.client.get(reverse("settings:asset-types"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prefix kodu")
        self.assertContains(response, "ST")

    def test_regular_user_cannot_access_list(self):
        user = User.objects.create_user(username="asset-type-user", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("settings:asset-types"))

        self.assertEqual(response.status_code, 403)

    def test_create_asset_type(self):
        self.client.force_login(self._manager_user())

        response = self.client.post(
            reverse("settings:asset-type-create"),
            data=self._form_data(name="Custom quantity", code="Custom Quantity", is_quantity_based="on"),
        )

        self.assertRedirects(response, reverse("settings:asset-types"))
        asset_type = AssetTypeDictionary.objects.get(code="custom-quantity")
        self.assertEqual(asset_type.name, "Custom quantity")
        self.assertTrue(asset_type.is_quantity_based)
        self.assertTrue(asset_type.is_active)
        self.assertEqual(asset_type.sort_order, 60)

    def test_create_asset_type_saves_barcode_prefix_uppercase(self):
        self.client.force_login(self._manager_user())

        response = self.client.post(
            reverse("settings:asset-type-create"),
            data=self._form_data(name="Prefix type", code="Prefix Type", barcode_prefix="p9"),
        )

        self.assertRedirects(response, reverse("settings:asset-types"))
        asset_type = AssetTypeDictionary.objects.get(code="prefix-type")
        self.assertEqual(asset_type.barcode_prefix, "P9")

    def test_create_asset_type_rejects_invalid_barcode_prefix(self):
        self.client.force_login(self._manager_user())

        response = self.client.post(
            reverse("settings:asset-type-create"),
            data=self._form_data(name="Bad prefix", code="Bad Prefix", barcode_prefix="A-1"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssetTypeDictionary.objects.filter(code="bad-prefix").exists())
        self.assertContains(response, "Prefix kodu musi miec 2-3 znaki")

    def test_update_asset_type(self):
        self.client.force_login(self._manager_user())
        asset_type = AssetTypeDictionary.objects.create(
            name="Editable",
            code="editable",
            sort_order=80,
        )

        response = self.client.post(
            reverse("settings:asset-type-update", kwargs={"pk": asset_type.pk}),
            data=self._form_data(name="Edited", code="edited", sort_order="90"),
        )

        self.assertRedirects(response, reverse("settings:asset-types"))
        asset_type.refresh_from_db()
        self.assertEqual(asset_type.name, "Edited")
        self.assertEqual(asset_type.code, "edited")
        self.assertEqual(asset_type.sort_order, 90)

    def test_deactivate_asset_type(self):
        self.client.force_login(self._manager_user())
        asset_type = AssetTypeDictionary.objects.get(code="fixed")

        response = self.client.post(reverse("settings:asset-type-deactivate", kwargs={"pk": asset_type.pk}))

        self.assertRedirects(response, reverse("settings:asset-types"))
        asset_type.refresh_from_db()
        self.assertFalse(asset_type.is_active)

    def test_activate_asset_type(self):
        self.client.force_login(self._manager_user())
        asset_type = AssetTypeDictionary.objects.get(code="fixed")
        asset_type.is_active = False
        asset_type.save(update_fields=["is_active", "updated_at"])

        response = self.client.post(reverse("settings:asset-type-activate", kwargs={"pk": asset_type.pk}))

        self.assertRedirects(response, reverse("settings:asset-types"))
        asset_type.refresh_from_db()
        self.assertTrue(asset_type.is_active)


class UserRequiresAssetChangeApprovalTests(TestCase):
    def test_superuser_does_not_require_approval(self):
        user = User.objects.create_superuser(
            username="approval-superuser",
            email="approval-superuser@example.com",
            password="test-pass-123",
        )

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_admin_role_with_approval_enabled_requires_approval(self):
        user = User.objects.create_user(username="approval-admin", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_admin_role_with_approval_disabled_does_not_require_approval(self):
        user = User.objects.create_user(username="approval-admin-disabled", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = False
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_user_with_approver_flag_and_approval_required_still_requires_approval(self):
        user = User.objects.create_user(username="approval-approver", password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["can_approve_asset_changes", "asset_changes_require_approval"])

        self.assertTrue(user_requires_asset_change_approval(user))

    def test_user_with_approval_disabled_does_not_require_approval(self):
        user = User.objects.create_user(username="approval-disabled", password="test-pass-123")

        self.assertFalse(user_requires_asset_change_approval(user))

    def test_user_with_approval_enabled_requires_approval(self):
        user = User.objects.create_user(username="approval-enabled", password="test-pass-123")
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])

        self.assertTrue(user_requires_asset_change_approval(user))

    def test_user_without_profile_requires_approval(self):
        user = User.objects.create_user(username="approval-no-profile", password="test-pass-123")
        user.profile.delete()

        self.assertTrue(user_requires_asset_change_approval(user))

    def test_anonymous_user_requires_approval(self):
        self.assertTrue(user_requires_asset_change_approval(AnonymousUser()))


class UserCanReviewAssetChangesTests(TestCase):
    def test_superuser_can_review(self):
        user = User.objects.create_superuser(
            username="review-superuser",
            email="review-superuser@example.com",
            password="test-pass-123",
        )

        self.assertTrue(_user_can_review_asset_changes(user))

    def test_manager_can_review(self):
        user = User.objects.create_user(username="review-manager", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])

        self.assertTrue(_user_can_review_asset_changes(user))

    def test_admin_role_without_approver_flag_can_review(self):
        user = User.objects.create_user(username="review-admin", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])

        self.assertTrue(_user_can_review_asset_changes(user))

    def test_user_with_legacy_approver_flag_cannot_review(self):
        user = User.objects.create_user(username="review-user-legacy-flag", password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.save(update_fields=["can_approve_asset_changes"])

        self.assertFalse(_user_can_review_asset_changes(user))


class SerializeAssetFormPayloadTests(TestCase):
    def test_preserves_json_primitive_values(self):
        payload = serialize_asset_form_payload(
            {
                "name": "Laptop",
                "is_active": True,
                "optional": None,
            }
        )

        self.assertEqual(payload["name"], "Laptop")
        self.assertIs(payload["is_active"], True)
        self.assertIsNone(payload["optional"])

    def test_serializes_decimal_to_string(self):
        payload = serialize_asset_form_payload({"purchase_value": Decimal("1234.50")})

        self.assertEqual(payload["purchase_value"], "1234.50")

    def test_serializes_date_to_iso_string(self):
        payload = serialize_asset_form_payload({"purchase_date": date(2026, 4, 27)})

        self.assertEqual(payload["purchase_date"], "2026-04-27")

    def test_serializes_datetime_to_iso_string(self):
        payload = serialize_asset_form_payload({"reviewed_at": datetime(2026, 4, 27, 12, 30, 15)})

        self.assertEqual(payload["reviewed_at"], "2026-04-27T12:30:15")

    def test_serializes_model_instance_to_pk(self):
        user = User.objects.create_user(username="payload-user", password="test-pass-123")

        payload = serialize_asset_form_payload({"responsible_person": user})

        self.assertEqual(payload["responsible_person"], user.pk)

    def test_serializes_mixed_list_values(self):
        user = User.objects.create_user(username="payload-list-user", password="test-pass-123")

        payload = serialize_asset_form_payload(
            {
                "values": [
                    "text",
                    Decimal("10.25"),
                    date(2026, 1, 2),
                    user,
                    None,
                ]
            }
        )

        self.assertEqual(payload["values"], ["text", "10.25", "2026-01-02", user.pk, None])

    def test_serializes_mixed_dict_values(self):
        user = User.objects.create_user(username="payload-dict-user", password="test-pass-123")

        payload = serialize_asset_form_payload(
            {
                "nested": {
                    "amount": Decimal("99.99"),
                    "date": date(2026, 2, 3),
                    "user": user,
                    "flag": False,
                }
            }
        )

        self.assertEqual(
            payload["nested"],
            {
                "amount": "99.99",
                "date": "2026-02-03",
                "user": user.pk,
                "flag": False,
            },
        )

    def test_falls_back_to_string_for_unusual_values(self):
        class UnusualValue:
            def __str__(self):
                return "unusual-value"

        payload = serialize_asset_form_payload({"custom": UnusualValue()})

        self.assertEqual(payload["custom"], "unusual-value")


class DeserializeAssetPayloadForFormTests(TestCase):
    def test_keeps_only_asset_form_fields(self):
        payload = {
            "name": "Laptop",
            "inventory_number": "DESERIALIZE-001",
            "malicious_field": "ignored",
            "id": 123,
            "created_at": "2026-04-27T12:00:00",
            "updated_at": "2026-04-27T12:30:00",
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data, {"name": "Laptop", "inventory_number": "DESERIALIZE-001"})

    def test_preserves_form_compatible_scalar_values(self):
        user = User.objects.create_user(username="deserialize-user", password="test-pass-123")
        payload = {
            "purchase_value": "1234.50",
            "purchase_date": "2026-04-27",
            "responsible_person": user.pk,
            "current_user": user.pk,
            "current_quantity": 9,
            "is_active": True,
            "record_quantity": 9,
            "last_inventory_date": "2026-05-01",
            "category": None,
            "manufacturer": "",
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data["purchase_value"], "1234.50")
        self.assertEqual(form_data["purchase_date"], "2026-04-27")
        self.assertEqual(form_data["responsible_person"], user.pk)
        self.assertEqual(form_data["current_user"], user.pk)
        self.assertEqual(form_data["current_quantity"], 9)
        self.assertNotIn("is_active", form_data)
        self.assertNotIn("record_quantity", form_data)
        self.assertNotIn("last_inventory_date", form_data)
        self.assertIsNone(form_data["category"])
        self.assertEqual(form_data["manufacturer"], "")

    def test_ignores_nested_values_outside_asset_form_fields(self):
        payload = {
            "name": "Laptop",
            "metadata": {"unexpected": True},
            "tags": ["unexpected"],
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data, {"name": "Laptop"})

    def test_does_not_create_asset(self):
        payload = {
            "name": "Not Created",
            "inventory_number": "DESERIALIZE-NO-CREATE-001",
            "asset_type": Asset.AssetType.FIXED,
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data["inventory_number"], "DESERIALIZE-NO-CREATE-001")
        self.assertFalse(Asset.objects.filter(inventory_number="DESERIALIZE-NO-CREATE-001").exists())

    def test_handles_flat_create_payload(self):
        location = Location.objects.create(name="Deserialize Warehouse")
        payload = {
            "name": "Create Payload",
            "inventory_number": "DESERIALIZE-CREATE-001",
            "asset_type": Asset.AssetType.LOW_VALUE,
            "location_fk": location.id,
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "review_comment": "ignored",
        }

        form_data = deserialize_asset_payload_for_form(payload)

        self.assertEqual(form_data["name"], "Create Payload")
        self.assertEqual(form_data["inventory_number"], "DESERIALIZE-CREATE-001")
        self.assertEqual(form_data["asset_type"], Asset.AssetType.LOW_VALUE)
        self.assertEqual(form_data["location_fk"], location.id)
        self.assertNotIn("review_comment", form_data)

    def test_handles_update_proposed_payload(self):
        location = Location.objects.create(name="Deserialize Updated")
        update_payload = {
            "current": {
                "name": "Old Name",
                "inventory_number": "DESERIALIZE-UPDATE-001",
            },
            "proposed": {
                "name": "New Name",
                "inventory_number": "DESERIALIZE-UPDATE-001",
                "location_fk": location.id,
                "unexpected": "ignored",
            },
        }

        form_data = deserialize_asset_payload_for_form(update_payload["proposed"])

        self.assertEqual(form_data["name"], "New Name")
        self.assertEqual(form_data["inventory_number"], "DESERIALIZE-UPDATE-001")
        self.assertEqual(form_data["location_fk"], location.id)
        self.assertNotIn("unexpected", form_data)


class ApproveAssetChangeRequestCreateTests(TestCase):
    def _create_payload(self, inventory_number="APPROVE-CREATE-001", **overrides):
        location, _ = Location.objects.get_or_create(name="Approve Create Location")
        payload = {
            "name": "Approved Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "category": "IT",
            "current_quantity": 1,
            "location_fk": location.id,
            "is_active": True,
        }
        payload.update(overrides)
        return payload

    def _create_request(self, requested_by, payload=None, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": AssetChangeRequest.Operation.CREATE,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": payload or self._create_payload(),
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_superuser_can_approve_pending_create_and_create_asset(self):
        requester = User.objects.create_user(username="approve-create-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-superuser",
            email="approve-create-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(current_quantity=6),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(asset.inventory_number, "APPROVE-CREATE-001")
        self.assertEqual(asset.name, "Approved Asset")
        self.assertEqual(asset.current_quantity, 6)
        self.assertIsNotNone(asset.location_fk)
        self.assertEqual(asset.location, asset.location_fk.path)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)
        self.assertEqual(change_request.asset, asset)

    def test_approval_create_creates_history_entry_with_reviewer_and_source(self):
        requester = User.objects.create_user(username="approve-create-history-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-history-reviewer",
            email="approve-create-history-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="APPROVE-CREATE-HISTORY-001"),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        entry = AssetHistoryEntry.objects.get(asset=asset)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.CREATED)
        self.assertEqual(entry.description, "Utworzono środek po zatwierdzeniu zmiany")
        self.assertEqual(entry.operator, reviewer)
        self.assertEqual(entry.source_object_type, "AssetChangeRequest")
        self.assertEqual(entry.source_object_id, change_request.id)

    def test_admin_role_without_approver_flag_can_approve_pending_create(self):
        requester = User.objects.create_user(username="approve-create-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="APPROVE-CREATE-ADMIN-001"),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(asset.inventory_number, "APPROVE-CREATE-ADMIN-001")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.asset, asset)
        self.assertEqual(change_request.reviewed_by, reviewer)

    def test_approver_without_global_access_cannot_approve_create_without_location_fk(self):
        requester = User.objects.create_user(username="approve-create-scope-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-scope-reviewer", password="test-pass-123")
        reviewer.profile.can_approve_asset_changes = True
        reviewer.profile.save(update_fields=["can_approve_asset_changes"])
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_regular_user_cannot_approve_create(self):
        requester = User.objects.create_user(username="approve-create-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-regular-reviewer", password="test-pass-123")
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_approved_request_cannot_be_approved_again(self):
        requester = User.objects.create_user(username="approve-create-approved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-approved-reviewer",
            email="approve-create-approved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_rejected_request_cannot_be_approved(self):
        requester = User.objects.create_user(username="approve-create-rejected-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-rejected-reviewer",
            email="approve-create-rejected-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.REJECTED,
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertIsNone(change_request.reviewed_by)
        self.assertIsNone(change_request.reviewed_at)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-001").exists())

    def test_invalid_payload_does_not_create_asset_or_change_status(self):
        requester = User.objects.create_user(username="approve-create-invalid-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-invalid-reviewer",
            email="approve-create-invalid-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(name="", inventory_number=""),
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertIsNone(change_request.reviewed_by)
        self.assertFalse(Asset.objects.exists())

    def test_update_operation_is_not_supported_yet(self):
        requester = User.objects.create_user(username="approve-update-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-reviewer",
            email="approve-update-reviewer@example.com",
            password="test-pass-123",
        )
        asset = Asset.objects.create(
            name="Existing Asset",
            inventory_number="APPROVE-UPDATE-EXISTING-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        change_request = AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": "Existing Asset"}, "proposed": {"name": "Updated Asset"}},
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(asset.name, "Existing Asset")

    def test_payload_is_validated_through_asset_form_and_extra_fields_are_ignored(self):
        requester = User.objects.create_user(username="approve-create-extra-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-extra-reviewer",
            email="approve-create-extra-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(
                inventory_number="APPROVE-CREATE-EXTRA-001",
                malicious_field="ignored",
                id=999,
                created_at="2026-04-27T12:00:00",
            ),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(asset.inventory_number, "APPROVE-CREATE-EXTRA-001")
        self.assertNotEqual(asset.pk, 999)
        self.assertFalse(hasattr(asset, "malicious_field"))

    def test_system_managed_fields_in_create_payload_are_ignored(self):
        requester = User.objects.create_user(username="approve-create-system-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-system-reviewer",
            email="approve-create-system-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(
                inventory_number="APPROVE-CREATE-SYSTEM-001",
                record_quantity=99,
                is_active=False,
                last_inventory_date="2026-05-01",
            ),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(asset.record_quantity, 1)
        self.assertTrue(asset.is_active)
        self.assertIsNone(asset.last_inventory_date)

    def test_unsaved_change_request_fails_with_controlled_error(self):
        requester = User.objects.create_user(username="approve-create-unsaved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-create-unsaved-reviewer",
            email="approve-create-unsaved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = AssetChangeRequest(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.CREATE,
            status=AssetChangeRequest.Status.PENDING,
            payload=self._create_payload(),
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        self.assertFalse(Asset.objects.exists())

    def test_manager_in_scope_can_approve_create(self):
        location = Location.objects.create(name="Approve Create Manager Location")
        requester = User.objects.create_user(username="approve-create-mgr-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-mgr-reviewer", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        reviewer.profile.allowed_locations.add(location)
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="APPROVE-CREATE-MGR-001", location_fk=location.id),
        )

        asset = approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(asset.inventory_number, "APPROVE-CREATE-MGR-001")
        self.assertEqual(asset.location_fk, location)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)
        self.assertEqual(change_request.asset, asset)

    def test_manager_outside_scope_cannot_approve_create(self):
        in_scope = Location.objects.create(name="Approve Create Manager In Scope")
        out_of_scope = Location.objects.create(name="Approve Create Manager Out Scope")
        requester = User.objects.create_user(username="approve-create-mgr-out-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-mgr-out-reviewer", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        reviewer.profile.allowed_locations.add(in_scope)
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="APPROVE-CREATE-MGR-OUT-001", location_fk=out_of_scope.id),
        )

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-MGR-OUT-001").exists())

    def test_manager_without_allowed_locations_cannot_approve_create(self):
        requester = User.objects.create_user(username="approve-create-mgr-noloc-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-create-mgr-noloc-reviewer", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="APPROVE-CREATE-MGR-NOLOC-001"),
        )

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertFalse(Asset.objects.filter(inventory_number="APPROVE-CREATE-MGR-NOLOC-001").exists())


class ApproveAssetChangeRequestUpdateTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Approve Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Approve Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number="APPROVE-UPDATE-001", location_obj=None, **overrides):
        defaults = {
            "name": "Original Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "category": "IT",
            "location": location_obj.path if location_obj else "Legacy location",
            "location_fk": location_obj,
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _current_payload(self, asset):
        values = {
            "name": asset.name,
            "inventory_number": asset.inventory_number,
            "asset_type": asset.asset_type,
            "category": asset.category,
            "manufacturer": asset.manufacturer,
            "model": asset.model,
            "serial_number": asset.serial_number,
            "barcode": asset.barcode,
            "description": asset.description,
            "purchase_date": asset.purchase_date,
            "commissioning_date": asset.commissioning_date,
            "purchase_value": asset.purchase_value,
            "invoice_number": asset.invoice_number,
            "external_id": asset.external_id,
            "cost_center": asset.cost_center,
            "organizational_unit": asset.organizational_unit,
            "department": asset.department,
            "location_fk": asset.location_fk,
            "room": asset.room,
            "responsible_person": asset.responsible_person,
            "current_user": asset.current_user,
            "current_quantity": asset.current_quantity,
            "status": asset.status,
            "technical_condition": asset.technical_condition,
            "last_inventory_date": asset.last_inventory_date,
            "next_review_date": asset.next_review_date,
            "warranty_until": asset.warranty_until,
            "insurance_until": asset.insurance_until,
            "is_active": asset.is_active,
        }
        return serialize_asset_form_payload(values)

    def _proposed_payload(self, asset, **overrides):
        payload = self._current_payload(asset)
        payload.update(
            {
                "name": "Approved Update",
                "asset_type": Asset.AssetType.LOW_VALUE,
                "status": Asset.Status.INACTIVE,
                "technical_condition": Asset.TechnicalCondition.VERY_GOOD,
            }
        )
        payload.update(overrides)
        return payload

    def _update_request(self, requester, asset, payload=None, **overrides):
        defaults = {
            "requested_by": requester,
            "operation": AssetChangeRequest.Operation.UPDATE,
            "status": AssetChangeRequest.Status.PENDING,
            "asset": asset,
            "payload": payload or {
                "current": self._current_payload(asset),
                "proposed": self._proposed_payload(asset),
            },
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def _manager_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        return user

    def _user_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.allowed_locations.add(location)
        return user

    def test_superuser_can_approve_update(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-super-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-super",
            email="approve-update-super@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(location_obj=location, current_quantity=2)
        change_request = self._update_request(
            requester,
            asset,
            payload={
                "current": self._current_payload(asset),
                "proposed": self._proposed_payload(asset, current_quantity=7),
            },
        )

        updated_asset = approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(updated_asset.name, "Approved Update")
        self.assertEqual(updated_asset.current_quantity, 7)
        self.assertEqual(updated_asset.status, Asset.Status.INACTIVE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)

    def test_approval_update_archived_asset_is_blocked(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-archived-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-archived-reviewer",
            email="approve-update-archived-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(
            inventory_number="APPROVE-UPDATE-ARCHIVED-001",
            location_obj=location,
            is_active=False,
            status=Asset.Status.LIQUIDATED,
        )
        change_request = self._update_request(requester, asset)

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_approval_update_creates_field_history_with_reviewer_and_source(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-history-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-history-reviewer",
            email="approve-update-history-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-HISTORY-001", location_obj=location)
        proposed = self._current_payload(asset)
        proposed["name"] = "Approval History Name"
        change_request = self._update_request(
            requester,
            asset,
            payload={"current": self._current_payload(asset), "proposed": proposed},
        )

        approve_asset_change_request(change_request, reviewer)

        entries = list(AssetHistoryEntry.objects.filter(asset=asset))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.UPDATED)
        self.assertEqual(entry.description, "Zmieniono nazwę")
        self.assertEqual(entry.field_name, "name")
        self.assertEqual(entry.old_value, "Original Asset")
        self.assertEqual(entry.new_value, "Approval History Name")
        self.assertEqual(entry.operator, reviewer)
        self.assertEqual(entry.source_object_type, "AssetChangeRequest")
        self.assertEqual(entry.source_object_id, change_request.id)
        self.assertNotIn("zatwierdzono", entry.description.lower())

    def test_approval_update_does_not_log_unmapped_or_quantity_audit_fields(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-unmapped-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-unmapped-reviewer",
            email="approve-update-unmapped-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-UNMAPPED-001", location_obj=location)
        proposed = self._current_payload(asset)
        proposed["description"] = "Opis techniczny bez historii"
        proposed["record_quantity"] = 7
        proposed["last_inventory_quantity"] = 3
        change_request = self._update_request(
            requester,
            asset,
            payload={"current": self._current_payload(asset), "proposed": proposed},
        )

        updated_asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(updated_asset.description, "Opis techniczny bez historii")
        self.assertEqual(updated_asset.record_quantity, 1)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())
        self.assertFalse(
            AssetHistoryEntry.objects.filter(
                asset=asset,
                field_name__in=["record_quantity", "last_inventory_quantity"],
            ).exists()
        )

    def test_admin_role_without_approver_flag_can_approve_update(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-update-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-ADMIN-001", location_obj=location)
        change_request = self._update_request(requester, asset)

        updated_asset = approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(updated_asset.name, "Approved Update")
        self.assertEqual(asset.name, "Approved Update")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)

    def test_manager_can_approve_update_in_scope(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-scope-requester", password="test-pass-123")
        reviewer = self._manager_with_location("approve-update-scope-manager", location)
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-SCOPE-001", location_obj=location)
        change_request = self._update_request(requester, asset)

        updated_asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(updated_asset.name, "Approved Update")
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_manager_cannot_approve_update_outside_scope(self):
        allowed_location, outside_location = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-outside-requester", password="test-pass-123")
        reviewer = self._manager_with_location("approve-update-outside-manager", allowed_location)
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-OUTSIDE-001", location_obj=outside_location)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_manager_cannot_approve_update_without_location_fk(self):
        allowed_location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-nofk-requester", password="test-pass-123")
        reviewer = self._manager_with_location("approve-update-nofk-manager", allowed_location)
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-NOFK-001", location_obj=None, location_fk=None)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_regular_user_cannot_approve_update(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="approve-update-regular-reviewer", password="test-pass-123")
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-REGULAR-001", location_obj=location)
        change_request = self._update_request(requester, asset)

        with self.assertRaises(PermissionDenied):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_update_request_without_asset_fails(self):
        requester = User.objects.create_user(username="approve-update-noasset-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-noasset-reviewer",
            email="approve-update-noasset-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=None,
            payload={"current": {}, "proposed": {}},
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_update_payload_without_current_fails(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-nocurrent-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-nocurrent-reviewer",
            email="approve-update-nocurrent-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-NOCURRENT-001", location_obj=location)
        change_request = self._update_request(requester, asset, payload={"proposed": self._proposed_payload(asset)})

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")

    def test_update_payload_without_proposed_fails(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-noproposed-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-noproposed-reviewer",
            email="approve-update-noproposed-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-NOPROPOSED-001", location_obj=location)
        change_request = self._update_request(requester, asset, payload={"current": self._current_payload(asset)})

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")

    def test_current_conflict_fails_without_saving(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-conflict-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-conflict-reviewer",
            email="approve-update-conflict-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-CONFLICT-001", location_obj=location)
        change_request = self._update_request(requester, asset)
        asset.name = "Changed Elsewhere"
        asset.save(update_fields=["name"])

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Changed Elsewhere")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_invalid_proposed_payload_fails_without_saving(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-invalid-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-invalid-reviewer",
            email="approve-update-invalid-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-INVALID-001", location_obj=location)
        change_request = self._update_request(
            requester,
            asset,
            payload={
                "current": self._current_payload(asset),
                "proposed": self._proposed_payload(asset, name="", inventory_number=""),
            },
        )

        with self.assertRaises(ValidationError):
            approve_asset_change_request(change_request, reviewer)

        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(asset.inventory_number, "APPROVE-UPDATE-INVALID-001")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_extra_fields_are_ignored_and_location_fk_updates_cache(self):
        location, _ = self._create_location_tree()
        target_location = Location.objects.create(name="Nowe biuro", parent=location)
        requester = User.objects.create_user(username="approve-update-extra-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-extra-reviewer",
            email="approve-update-extra-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(inventory_number="APPROVE-UPDATE-EXTRA-001", location_obj=location)
        proposed = self._proposed_payload(
            asset,
            name="Extra Ignored Update",
            location_fk=target_location.id,
            malicious_field="ignored",
            id=999,
        )
        change_request = self._update_request(
            requester,
            asset,
            payload={"current": self._current_payload(asset), "proposed": proposed},
        )

        updated_asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(updated_asset.name, "Extra Ignored Update")
        self.assertEqual(updated_asset.location_fk, target_location)
        self.assertEqual(updated_asset.location, target_location.path)
        self.assertNotEqual(updated_asset.pk, 999)
        self.assertFalse(hasattr(updated_asset, "malicious_field"))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_system_managed_fields_in_update_payload_are_ignored(self):
        location, _ = self._create_location_tree()
        requester = User.objects.create_user(username="approve-update-system-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="approve-update-system-reviewer",
            email="approve-update-system-reviewer@example.com",
            password="test-pass-123",
        )
        asset = self._create_asset(
            inventory_number="APPROVE-UPDATE-SYSTEM-001",
            location_obj=location,
            record_quantity=4,
            is_active=True,
            last_inventory_date=date(2026, 4, 1),
        )
        proposed = self._proposed_payload(
            asset,
            record_quantity=99,
            is_active=False,
            last_inventory_date="2026-05-01",
        )
        change_request = self._update_request(
            requester,
            asset,
            payload={"current": self._current_payload(asset), "proposed": proposed},
        )

        updated_asset = approve_asset_change_request(change_request, reviewer)

        self.assertEqual(updated_asset.name, "Approved Update")
        self.assertEqual(updated_asset.record_quantity, 4)
        self.assertTrue(updated_asset.is_active)
        self.assertEqual(updated_asset.last_inventory_date, date(2026, 4, 1))


class RejectAssetChangeRequestTests(TestCase):
    def _create_payload(self, inventory_number="REJECT-CREATE-001"):
        return {
            "name": "Rejected Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
        }

    def _create_request(self, requested_by, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": AssetChangeRequest.Operation.CREATE,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": self._create_payload(),
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_superuser_can_reject_pending_create(self):
        requester = User.objects.create_user(username="reject-superuser-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-superuser",
            email="reject-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(requester)

        rejected_request = reject_asset_change_request(change_request, reviewer, comment="Not enough data")

        change_request.refresh_from_db()
        self.assertEqual(rejected_request.pk, change_request.pk)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertIsNotNone(change_request.reviewed_at)
        self.assertEqual(change_request.review_comment, "Not enough data")
        self.assertIsNone(change_request.asset)

    def test_admin_role_without_approver_flag_can_reject_pending_create(self):
        requester = User.objects.create_user(username="reject-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="reject-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_request(requester)

        reject_asset_change_request(change_request, reviewer, comment="Rejected by admin")

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertEqual(change_request.review_comment, "Rejected by admin")

    def test_manager_can_reject_pending_create(self):
        requester = User.objects.create_user(username="reject-manager-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="reject-manager", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_request(requester)

        reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertEqual(change_request.review_comment, "")

    def test_regular_user_cannot_reject(self):
        requester = User.objects.create_user(username="reject-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="reject-regular", password="test-pass-123")
        change_request = self._create_request(requester)

        with self.assertRaises(PermissionDenied):
            reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.reviewed_by)

    def test_approved_request_cannot_be_rejected(self):
        requester = User.objects.create_user(username="reject-approved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-approved-reviewer",
            email="reject-approved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )

        with self.assertRaises(ValidationError):
            reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)

    def test_rejected_request_cannot_be_rejected_again(self):
        requester = User.objects.create_user(username="reject-rejected-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-rejected-reviewer",
            email="reject-rejected-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.REJECTED,
            reviewed_by=reviewer,
            review_comment="Already rejected",
        )

        with self.assertRaises(ValidationError):
            reject_asset_change_request(change_request, reviewer, comment="Second rejection")

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.review_comment, "Already rejected")

    def test_unsaved_change_request_fails_with_controlled_error(self):
        requester = User.objects.create_user(username="reject-unsaved-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-unsaved-reviewer",
            email="reject-unsaved-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = AssetChangeRequest(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.CREATE,
            status=AssetChangeRequest.Status.PENDING,
            payload=self._create_payload(),
        )

        with self.assertRaises(ValidationError):
            reject_asset_change_request(change_request, reviewer)

    def test_reject_does_not_create_asset_for_create_request(self):
        requester = User.objects.create_user(username="reject-no-create-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-no-create-reviewer",
            email="reject-no-create-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_request(
            requester,
            payload=self._create_payload(inventory_number="REJECT-NO-CREATE-001"),
        )

        reject_asset_change_request(change_request, reviewer)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertIsNone(change_request.asset)
        self.assertFalse(Asset.objects.filter(inventory_number="REJECT-NO-CREATE-001").exists())

    def test_reject_does_not_change_asset_for_update_request(self):
        requester = User.objects.create_user(username="reject-update-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="reject-update-reviewer",
            email="reject-update-reviewer@example.com",
            password="test-pass-123",
        )
        asset = Asset.objects.create(
            name="Original Asset",
            inventory_number="REJECT-UPDATE-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        change_request = AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": "Original Asset"}, "proposed": {"name": "Rejected Update"}},
        )

        reject_asset_change_request(change_request, reviewer, comment="No update")

        change_request.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.asset, asset)
        self.assertEqual(asset.name, "Original Asset")
        self.assertEqual(change_request.review_comment, "No update")


class AssetChangeRequestModelTests(TestCase):
    def test_can_create_request_without_asset(self):
        user = User.objects.create_user(username="requester-create", password="test-pass-123")

        request = AssetChangeRequest.objects.create(
            requested_by=user,
            operation=AssetChangeRequest.Operation.CREATE,
            payload={
                "name": "New Laptop",
                "inventory_number": "NEW-001",
                "attributes": {"manufacturer": "Dell"},
            },
        )

        self.assertIsNone(request.asset)
        self.assertEqual(request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(request.payload["name"], "New Laptop")
        self.assertEqual(request.payload["attributes"]["manufacturer"], "Dell")

    def test_can_create_update_request_with_asset(self):
        user = User.objects.create_user(username="requester-update", password="test-pass-123")
        asset = Asset.objects.create(
            name="Existing Laptop",
            inventory_number="UPD-001",
            status=Asset.Status.ACTIVE,
            location="HQ",
            category="IT",
        )

        request = AssetChangeRequest.objects.create(
            requested_by=user,
            operation=AssetChangeRequest.Operation.UPDATE,
            asset=asset,
            payload={"status": Asset.Status.INACTIVE, "location": "HQ / Room 1"},
        )

        self.assertEqual(request.asset, asset)
        self.assertEqual(request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(request.payload["status"], Asset.Status.INACTIVE)
        self.assertEqual(request.payload["location"], "HQ / Room 1")


class AssetChangeRequestListViewTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Queue Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Queue Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            status=Asset.Status.ACTIVE,
            location=location.path if location else "Legacy only",
            location_fk=location,
            category="IT",
        )

    def _create_change_request(self, requested_by, operation, marker, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": operation,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": {"name": marker, "inventory_number": marker},
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_change_list_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_regular_user_sees_only_own_requests(self):
        user = User.objects.create_user(username="queue-regular-user", password="test-pass-123")
        other_user = User.objects.create_user(username="queue-other-user", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Operation.CREATE, "QUEUE-OWN-REQUEST")
        self._create_change_request(other_user, AssetChangeRequest.Operation.CREATE, "QUEUE-OTHER-REQUEST")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-OWN-REQUEST")
        self.assertNotContains(response, "QUEUE-OTHER-REQUEST")

    def test_regular_user_status_filters_apply_to_own_requests(self):
        user = User.objects.create_user(username="queue-regular-filter-user", password="test-pass-123")
        other_user = User.objects.create_user(username="queue-regular-filter-other", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Operation.CREATE, "QUEUE-OWN-PENDING")
        self._create_change_request(
            user,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-OWN-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self._create_change_request(
            other_user,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-OTHER-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.APPROVED})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-OWN-APPROVED")
        self.assertNotContains(response, "QUEUE-OWN-PENDING")
        self.assertNotContains(response, "QUEUE-OTHER-APPROVED")

    def test_superuser_sees_pending_create_and_update_requests(self):
        requester = User.objects.create_user(username="queue-super-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-superuser",
            email="queue-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-SUPER-UPD-001", location)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-SUPER-CREATE-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-SUPER-UPDATE-001",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-SUPER-UPDATE-001"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-SUPER-CREATE-001")
        self.assertContains(response, "QUEUE-SUPER-UPD-001")

    def test_admin_role_without_approver_flag_sees_global_change_list(self):
        requester = User.objects.create_user(username="queue-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="queue-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-ADMIN-UPD-001", location)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-ADMIN-CREATE-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-ADMIN-UPDATE-001",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-ADMIN-UPDATE-001"}},
        )
        self._create_change_request(reviewer, AssetChangeRequest.Operation.CREATE, "QUEUE-ADMIN-OWN-REQUEST")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-ADMIN-OWN-REQUEST")
        self.assertContains(response, "QUEUE-ADMIN-CREATE-001")
        self.assertContains(response, "QUEUE-ADMIN-UPD-001")

    def test_manager_sees_only_update_requests_in_scope(self):
        requester = User.objects.create_user(username="queue-scope-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="queue-scope-manager", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        allowed_location, outside_location = self._create_location_tree()
        reviewer.profile.allowed_locations.add(allowed_location)
        in_scope_asset = self._create_asset("QUEUE-IN-SCOPE-001", allowed_location)
        outside_asset = self._create_asset("QUEUE-OUT-SCOPE-001", outside_location)
        null_location_asset = self._create_asset("QUEUE-NO-FK-001", None)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-CREATE-NO-FK-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-IN-SCOPE-UPDATE",
            asset=in_scope_asset,
            payload={"current": {"name": in_scope_asset.name}, "proposed": {"name": "QUEUE-IN-SCOPE-UPDATE"}},
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-OUT-SCOPE-UPDATE",
            asset=outside_asset,
            payload={"current": {"name": outside_asset.name}, "proposed": {"name": "QUEUE-OUT-SCOPE-UPDATE"}},
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-NO-FK-UPDATE",
            asset=null_location_asset,
            payload={"current": {"name": null_location_asset.name}, "proposed": {"name": "QUEUE-NO-FK-UPDATE"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-IN-SCOPE-001")
        self.assertNotContains(response, "QUEUE-OUT-SCOPE-001")
        self.assertNotContains(response, "QUEUE-NO-FK-001")
        self.assertNotContains(response, "QUEUE-CREATE-NO-FK-001")

    def test_manager_sees_create_request_with_location_in_scope(self):
        requester = User.objects.create_user(username="queue-create-scope-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="queue-create-scope-manager", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        allowed_location, outside_location = self._create_location_tree()
        reviewer.profile.allowed_locations.add(allowed_location)
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-CREATE-IN-SCOPE-001",
            payload={
                "name": "QUEUE-CREATE-IN-SCOPE-001",
                "inventory_number": "QUEUE-CREATE-IN-SCOPE-001",
                "location_fk": allowed_location.id,
            },
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-CREATE-OUT-SCOPE-001",
            payload={
                "name": "QUEUE-CREATE-OUT-SCOPE-001",
                "inventory_number": "QUEUE-CREATE-OUT-SCOPE-001",
                "location_fk": outside_location.id,
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QUEUE-CREATE-IN-SCOPE-001")
        self.assertNotContains(response, "QUEUE-CREATE-OUT-SCOPE-001")

    def test_manager_without_allowed_locations_sees_empty_queue(self):
        requester = User.objects.create_user(username="queue-noloc-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="queue-noloc-manager", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.MANAGER
        reviewer.profile.save(update_fields=["role"])
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-NOLOC-ASSET-001", allowed_location)
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NOLOC-CREATE",
            payload={"name": "QUEUE-NOLOC-CREATE", "inventory_number": "QUEUE-NOLOC-CREATE", "location_fk": allowed_location.id},
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-NOLOC-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-NOLOC-UPDATE"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak zmian do wyświetlenia.")

    def test_default_list_shows_only_pending_requests(self):
        requester = User.objects.create_user(username="queue-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-status-superuser",
            email="queue-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-PENDING-001")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVED-001",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "QUEUE-PENDING-001")
        self.assertNotContains(response, "QUEUE-APPROVED-001")

    def test_status_filters(self):
        requester = User.objects.create_user(username="queue-filter-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-filter-status-superuser",
            email="queue-filter-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-FILTER-PENDING")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-FILTER-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(reviewer)

        approved_response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.APPROVED})
        all_response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertContains(approved_response, "QUEUE-FILTER-APPROVED")
        self.assertNotContains(approved_response, "QUEUE-FILTER-PENDING")
        self.assertContains(all_response, "QUEUE-FILTER-APPROVED")
        self.assertContains(all_response, "QUEUE-FILTER-PENDING")

    def test_change_list_translates_pending_status(self):
        requester = User.objects.create_user(username="queue-pending-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-pending-status-superuser",
            email="queue-pending-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-PENDING-STATUS")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "<div>Oczekuje</div>", html=True)

    def test_change_list_translates_approved_status(self):
        requester = User.objects.create_user(username="queue-approved-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-approved-status-superuser",
            email="queue-approved-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVED-STATUS",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.APPROVED})

        self.assertContains(response, "<div>Zatwierdzone</div>", html=True)

    def test_change_list_translates_rejected_status(self):
        requester = User.objects.create_user(username="queue-rejected-status-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-rejected-status-superuser",
            email="queue-rejected-status-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECTED-STATUS",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.REJECTED})

        self.assertContains(response, "<div>Odrzucone</div>", html=True)

    def test_change_list_shows_rejected_comment(self):
        requester = User.objects.create_user(username="queue-rejected-comment-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-rejected-comment-superuser",
            email="queue-rejected-comment-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECTED-COMMENT",
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="Błędna wartość",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.REJECTED})

        self.assertContains(response, 'class="asset-change-review-comment"')
        self.assertContains(response, 'title="Błędna wartość"')
        self.assertContains(response, "Powód: Błędna wartość")

    def test_change_list_does_not_show_rejected_reason_without_comment(self):
        requester = User.objects.create_user(username="queue-rejected-empty-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-rejected-empty-superuser",
            email="queue-rejected-empty-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECTED-EMPTY",
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.REJECTED})

        self.assertContains(response, "<div>Odrzucone</div>", html=True)
        self.assertNotContains(response, "Powód")

    def test_operation_filters(self):
        requester = User.objects.create_user(username="queue-filter-operation-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-filter-operation-superuser",
            email="queue-filter-operation-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-FILTER-UPD-ASSET", location)
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-FILTER-CREATE")
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-FILTER-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "QUEUE-FILTER-UPDATE"}},
        )
        self.client.force_login(reviewer)

        create_response = self.client.get(reverse("assets:change-list"), {"operation": AssetChangeRequest.Operation.CREATE})
        update_response = self.client.get(reverse("assets:change-list"), {"operation": AssetChangeRequest.Operation.UPDATE})

        self.assertContains(create_response, "QUEUE-FILTER-CREATE")
        self.assertNotContains(create_response, "QUEUE-FILTER-UPD-ASSET")
        self.assertContains(update_response, "QUEUE-FILTER-UPD-ASSET")
        self.assertNotContains(update_response, "QUEUE-FILTER-CREATE")

    def test_change_list_shows_changes_column_and_hides_review_columns(self):
        requester = User.objects.create_user(username="queue-columns-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-columns-superuser",
            email="queue-columns-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-COLUMNS-CREATE")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<th>Zmiany</th>", html=True)
        self.assertNotContains(response, "<th>Sprawdził</th>", html=True)
        self.assertNotContains(response, "<th>Sprawdzono</th>", html=True)

    def test_change_list_shows_update_changed_fields(self):
        requester = User.objects.create_user(username="queue-diff-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-diff-superuser",
            email="queue-diff-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-DIFF-ASSET", location)
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-DIFF-UPDATE",
            asset=asset,
            payload={
                "current": {"name": "Old name", "inventory_number": "OLD-001"},
                "proposed": {"name": "New name", "inventory_number": "NEW-001"},
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, 'class="asset-change-diff"')
        self.assertContains(response, 'class="asset-change-diff-label">Nazwa</div>')
        self.assertContains(response, 'class="asset-change-diff-old">Old name</span>')
        self.assertContains(response, 'class="asset-change-diff-new">New name</span>')
        self.assertContains(response, 'class="asset-change-diff-label">Nr inw.</div>')
        self.assertContains(response, 'class="asset-change-diff-old">OLD-001</span>')
        self.assertContains(response, 'class="asset-change-diff-new">NEW-001</span>')

    def test_change_list_limits_update_changes_to_three_fields(self):
        requester = User.objects.create_user(username="queue-diff-limit-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-diff-limit-superuser",
            email="queue-diff-limit-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("QUEUE-DIFF-LIMIT-ASSET", location)
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "QUEUE-DIFF-LIMIT-UPDATE",
            asset=asset,
            payload={
                "current": {
                    "name": "Old name",
                    "inventory_number": "OLD-001",
                    "value": 1000,
                    "location": "Old location",
                    "status": "active",
                },
                "proposed": {
                    "name": "New name",
                    "inventory_number": "NEW-001",
                    "value": 1200,
                    "location": "New location",
                    "status": "inactive",
                },
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, 'class="asset-change-diff-label">Nazwa</div>')
        self.assertContains(response, 'class="asset-change-diff-old">Old name</span>')
        self.assertContains(response, 'class="asset-change-diff-new">New name</span>')
        self.assertContains(response, 'class="asset-change-diff-label">Nr inw.</div>')
        self.assertContains(response, 'class="asset-change-diff-old">OLD-001</span>')
        self.assertContains(response, 'class="asset-change-diff-new">NEW-001</span>')
        self.assertContains(response, 'class="asset-change-diff-old">1000</span>')
        self.assertContains(response, 'class="asset-change-diff-new">1200</span>')
        self.assertContains(response, "+ 2 innych zmian")
        self.assertNotContains(response, "Lokalizacja: Old location → New location")
        self.assertNotContains(response, "Status: active → inactive")

    def test_change_list_shows_create_summary(self):
        requester = User.objects.create_user(username="queue-create-summary-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-create-summary-superuser",
            email="queue-create-summary-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(requester, AssetChangeRequest.Operation.CREATE, "QUEUE-CREATE-SUMMARY")
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        self.assertContains(response, "Nowy składnik")

    def test_change_list_shows_bulk_approve_button_for_pending_request(self):
        requester = User.objects.create_user(username="queue-approve-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-approve-button-superuser",
            email="queue-approve-button-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVE-BUTTON",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        approve_url = reverse("assets:change-approve", kwargs={"pk": change_request.pk})
        self.assertNotContains(response, "<th>Akcje</th>", html=True)
        self.assertNotContains(response, f'action="{approve_url}"')
        self.assertContains(response, 'data-role="bulk-approve"')
        self.assertContains(response, reverse("assets:bulk-approve"))
        self.assertContains(response, "Zatwierdź")

    def test_change_list_shows_bulk_reject_action_for_pending_request(self):
        requester = User.objects.create_user(username="queue-reject-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-reject-button-superuser",
            email="queue-reject-button-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECT-BUTTON",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"))

        reject_url = reverse("assets:change-reject", kwargs={"pk": change_request.pk})
        self.assertNotContains(response, f'action="{reject_url}"')
        self.assertNotContains(response, 'data-role="toggle-reject-form"')
        self.assertNotContains(response, f'data-target="reject-form-{change_request.pk}"')
        self.assertNotContains(response, f'id="reject-form-{change_request.pk}"')
        self.assertContains(response, 'data-role="bulk-reject"')
        self.assertContains(response, 'data-role="bulk-reject-confirm"')
        self.assertContains(response, reverse("assets:bulk-reject"))
        self.assertContains(response, "Odrzu")

    def test_change_list_hides_approve_button_for_non_pending_requests(self):
        requester = User.objects.create_user(username="queue-no-approve-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-no-approve-button-superuser",
            email="queue-no-approve-button-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-APPROVE-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-APPROVE-REJECTED",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertContains(response, "QUEUE-NO-APPROVE-APPROVED")
        self.assertContains(response, "QUEUE-NO-APPROVE-REJECTED")
        self.assertNotContains(response, "change-approve")

    def test_change_list_hides_reject_action_for_non_pending_requests(self):
        requester = User.objects.create_user(username="queue-no-reject-button-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-no-reject-button-superuser",
            email="queue-no-reject-button-superuser@example.com",
            password="test-pass-123",
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-REJECT-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
        )
        self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-NO-REJECT-REJECTED",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertContains(response, "QUEUE-NO-REJECT-APPROVED")
        self.assertContains(response, "QUEUE-NO-REJECT-REJECTED")
        self.assertNotContains(response, "change-reject")
        self.assertNotContains(response, 'name="comment"')

    def test_change_list_approve_post_approves_request(self):
        requester = User.objects.create_user(username="queue-approve-post-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-approve-post-superuser",
            email="queue-approve-post-superuser@example.com",
            password="test-pass-123",
        )
        location = Location.objects.create(name="Queue Approve Location")
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-APPROVE-POST-001",
            payload={
                "name": "Queue Approve Post",
                "inventory_number": "QUEUE-APPROVE-POST-001",
                "asset_type": Asset.AssetType.LOW_VALUE,
                "category": "IT",
                "status": Asset.Status.ACTIVE,
                "technical_condition": Asset.TechnicalCondition.GOOD,
                "current_quantity": 1,
                "location_fk": location.id,
                "is_active": True,
            },
        )
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        change_request.refresh_from_db()
        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertTrue(Asset.objects.filter(inventory_number="QUEUE-APPROVE-POST-001").exists())

    def test_change_list_reject_post_rejects_request_with_comment(self):
        requester = User.objects.create_user(username="queue-reject-post-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="queue-reject-post-superuser",
            email="queue-reject-post-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "QUEUE-REJECT-POST-001",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Rejected from list"},
        )

        change_request.refresh_from_db()
        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.review_comment, "Rejected from list")

    def test_change_list_paginate_by_50(self):
        self.assertEqual(AssetChangeRequestListView.paginate_by, 50)

    def test_pending_change_request_has_enabled_checkbox(self):
        requester = User.objects.create_user(username="checkbox-pending-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="checkbox-pending-reviewer",
            email="checkbox-pending-reviewer@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "CHECKBOX-PENDING-001",
            status=AssetChangeRequest.Status.PENDING,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": AssetChangeRequest.Status.PENDING})

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'<input type="checkbox" data-role="change-select" value="{change_request.id}" aria-label="Zaznacz zgłoszenie {change_request.id}">',
            html=True,
        )

    def test_approved_and_rejected_change_requests_have_disabled_checkboxes(self):
        requester = User.objects.create_user(username="checkbox-nonpending-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="checkbox-nonpending-reviewer",
            email="checkbox-nonpending-reviewer@example.com",
            password="test-pass-123",
        )
        approved_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "CHECKBOX-APPROVED-001",
            status=AssetChangeRequest.Status.APPROVED,
        )
        rejected_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "CHECKBOX-REJECTED-001",
            status=AssetChangeRequest.Status.REJECTED,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-list"), {"status": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'value="{approved_request.id}" aria-label="Zaznacz zgłoszenie {approved_request.id}" disabled')
        self.assertContains(response, f'value="{rejected_request.id}" aria-label="Zaznacz zgłoszenie {rejected_request.id}" disabled')


class AssetChangeRequestDetailViewTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Detail Queue Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Detail Queue Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Detail Asset {inventory_number}",
            inventory_number=inventory_number,
            status=Asset.Status.ACTIVE,
            location=location.path if location else "Legacy only",
            location_fk=location,
            category="IT",
        )

    def _create_change_request(self, requested_by, operation, marker, **overrides):
        defaults = {
            "requested_by": requested_by,
            "operation": operation,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": {"name": marker, "inventory_number": marker},
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def _manager_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        return user

    def test_change_detail_redirects_anonymous_user_to_login(self):
        requester = User.objects.create_user(username="change-detail-anon-requester", password="test-pass-123")
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CHANGE-ANON-001",
        )

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_regular_user_gets_403_for_change_detail(self):
        requester = User.objects.create_user(username="change-detail-regular-requester", password="test-pass-123")
        user = User.objects.create_user(username="change-detail-regular", password="test-pass-123")
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CHANGE-REGULAR-001",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)

    def test_superuser_sees_create_request_detail(self):
        requester = User.objects.create_user(username="change-detail-super-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-superuser",
            email="change-detail-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CREATE-SUPER-001",
            payload={"name": "Detail Create Asset", "inventory_number": "DETAIL-CREATE-SUPER-001"},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "asset-change-diff-table")
        self.assertContains(response, "Detail Create Asset")
        self.assertContains(response, "DETAIL-CREATE-SUPER-001")

    def test_admin_role_without_approver_flag_sees_change_detail(self):
        requester = User.objects.create_user(username="change-detail-admin-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="change-detail-admin", password="test-pass-123")
        reviewer.profile.role = UserProfile.Role.ADMIN
        reviewer.profile.save(update_fields=["role"])
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CREATE-ADMIN-001",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DETAIL-CREATE-ADMIN-001")

    def test_manager_sees_update_request_in_scope(self):
        requester = User.objects.create_user(username="change-detail-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._manager_with_location("change-detail-scope-manager", allowed_location)
        asset = self._create_asset("DETAIL-SCOPE-ASSET-001", allowed_location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-SCOPE-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Detail Scope Updated"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Detail Scope Updated")

    def test_manager_gets_404_for_update_outside_scope(self):
        requester = User.objects.create_user(username="change-detail-outside-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._manager_with_location("change-detail-outside-manager", allowed_location)
        asset = self._create_asset("DETAIL-OUTSIDE-ASSET-001", outside_location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-OUTSIDE-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Outside Updated"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)

    def test_manager_gets_404_for_update_without_location_fk(self):
        requester = User.objects.create_user(username="change-detail-nofk-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._manager_with_location("change-detail-nofk-manager", allowed_location)
        asset = self._create_asset("DETAIL-NOFK-ASSET-001", None)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-NOFK-UPDATE",
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "No FK Updated"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)

    def test_manager_gets_404_for_create_without_location_fk(self):
        requester = User.objects.create_user(username="change-detail-create-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._manager_with_location("change-detail-create-scope-manager", allowed_location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-CREATE-SCOPE-HIDDEN",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)

    def test_update_detail_shows_only_changed_fields(self):
        requester = User.objects.create_user(username="change-detail-diff-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-diff-superuser",
            email="change-detail-diff-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("DETAIL-DIFF-ASSET-001", location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-DIFF-UPDATE",
            asset=asset,
            payload={
                "current": {"name": "Old Name", "status": Asset.Status.ACTIVE, "category": "Same"},
                "proposed": {"name": "New Name", "status": Asset.Status.INACTIVE, "category": "Same"},
            },
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertContains(response, 'class="asset-change-diff-table"')
        self.assertContains(response, "<th>Pole</th>", html=True)
        self.assertContains(response, "<th>Było</th>", html=True)
        self.assertContains(response, "<th>Jest</th>", html=True)
        self.assertContains(response, "Old Name")
        self.assertContains(response, "New Name")
        self.assertContains(response, Asset.Status.ACTIVE)
        self.assertContains(response, Asset.Status.INACTIVE)
        self.assertNotContains(response, "<td>category</td>", html=True)

    def test_update_detail_without_differences_shows_message(self):
        requester = User.objects.create_user(username="change-detail-nodiff-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-nodiff-superuser",
            email="change-detail-nodiff-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("DETAIL-NODIFF-ASSET-001", location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-NODIFF-UPDATE",
            asset=asset,
            payload={"current": {"name": "Same Name"}, "proposed": {"name": "Same Name"}},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak różnic w payloadzie.")

    def test_broken_update_payload_does_not_break_detail_view(self):
        requester = User.objects.create_user(username="change-detail-broken-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-broken-superuser",
            email="change-detail-broken-superuser@example.com",
            password="test-pass-123",
        )
        location, _ = self._create_location_tree()
        asset = self._create_asset("DETAIL-BROKEN-ASSET-001", location)
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.UPDATE,
            "DETAIL-BROKEN-UPDATE",
            asset=asset,
            payload={"current": "not-a-dict"},
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak różnic w payloadzie.")

    def test_pending_detail_shows_approve_form_with_action_url(self):
        requester = User.objects.create_user(username="change-detail-approve-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-approve-form-superuser",
            email="change-detail-approve-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-APPROVE-FORM-001",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        approve_url = reverse("assets:change-approve", kwargs={"pk": change_request.pk})
        self.assertContains(response, 'class="asset-change-approve-form"')
        self.assertContains(response, f'action="{approve_url}"')
        self.assertContains(response, "Zatwierdź")

    def test_pending_detail_shows_reject_form_with_action_url_and_comment_field(self):
        requester = User.objects.create_user(username="change-detail-reject-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-reject-form-superuser",
            email="change-detail-reject-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-REJECT-FORM-001",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        reject_url = reverse("assets:change-reject", kwargs={"pk": change_request.pk})
        self.assertContains(response, 'class="asset-change-reject-form"')
        self.assertContains(response, f'action="{reject_url}"')
        self.assertContains(response, 'name="comment"')
        self.assertContains(response, "Komentarz do odrzucenia")
        self.assertContains(response, "Odrzuć")

    def test_approved_detail_does_not_show_approval_forms(self):
        requester = User.objects.create_user(username="change-detail-approved-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-approved-form-superuser",
            email="change-detail-approved-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-APPROVED-NO-FORMS-001",
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertNotContains(response, "asset-change-approve-form")
        self.assertNotContains(response, "asset-change-reject-form")
        self.assertNotContains(response, reverse("assets:change-approve", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, reverse("assets:change-reject", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, 'name="comment"')

    def test_rejected_detail_does_not_show_approval_forms(self):
        requester = User.objects.create_user(username="change-detail-rejected-form-requester", password="test-pass-123")
        reviewer = User.objects.create_superuser(
            username="change-detail-rejected-form-superuser",
            email="change-detail-rejected-form-superuser@example.com",
            password="test-pass-123",
        )
        change_request = self._create_change_request(
            requester,
            AssetChangeRequest.Operation.CREATE,
            "DETAIL-REJECTED-NO-FORMS-001",
            status=AssetChangeRequest.Status.REJECTED,
            reviewed_by=reviewer,
            review_comment="Already rejected",
        )
        self.client.force_login(reviewer)

        response = self.client.get(reverse("assets:change-detail", kwargs={"pk": change_request.pk}))

        self.assertNotContains(response, "asset-change-approve-form")
        self.assertNotContains(response, "asset-change-reject-form")
        self.assertNotContains(response, reverse("assets:change-approve", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, reverse("assets:change-reject", kwargs={"pk": change_request.pk}))
        self.assertNotContains(response, 'name="comment"')


class AssetChangeRequestPostWorkflowViewTests(TestCase):
    def _create_location_tree(self):
        root_location = Location.objects.create(name="Post Workflow Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Post Workflow Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _admin_user(self, username):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])
        return user

    def _superuser(self, username):
        return User.objects.create_superuser(
            username=username,
            email=f"{username}@example.com",
            password="test-pass-123",
        )

    def _manager_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        return user

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Post Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=Asset.AssetType.FIXED,
            category="IT",
            location=location.path if location else "Legacy only",
            location_fk=location,
            status=Asset.Status.ACTIVE,
            technical_condition=Asset.TechnicalCondition.GOOD,
            is_active=True,
        )

    def _asset_payload(self, asset):
        return serialize_asset_form_payload(
            {
                field_name: getattr(asset, field_name)
                for field_name in AssetForm.Meta.fields
            }
        )

    def _create_payload(self, inventory_number):
        location, _ = Location.objects.get_or_create(name="Post Create Location")
        return {
            "name": f"Post Create {inventory_number}",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "category": "IT",
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "current_quantity": 1,
            "location_fk": location.id,
            "is_active": True,
        }

    def _create_request(self, requester, inventory_number="POST-CREATE-001", **overrides):
        defaults = {
            "requested_by": requester,
            "operation": AssetChangeRequest.Operation.CREATE,
            "status": AssetChangeRequest.Status.PENDING,
            "payload": self._create_payload(inventory_number),
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def _update_request(self, requester, asset, proposed_name="Post Updated Asset", **overrides):
        current = self._asset_payload(asset)
        proposed = current.copy()
        proposed["name"] = proposed_name
        defaults = {
            "requested_by": requester,
            "operation": AssetChangeRequest.Operation.UPDATE,
            "status": AssetChangeRequest.Status.PENDING,
            "asset": asset,
            "payload": {"current": current, "proposed": proposed},
        }
        defaults.update(overrides)
        return AssetChangeRequest.objects.create(**defaults)

    def test_superuser_can_approve_create(self):
        requester = User.objects.create_user(username="post-approve-create-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-create-superuser")
        change_request = self._create_request(requester, inventory_number="POST-APPROVE-CREATE-001")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertTrue(Asset.objects.filter(inventory_number="POST-APPROVE-CREATE-001").exists())

    def test_superuser_can_approve_update(self):
        requester = User.objects.create_user(username="post-approve-update-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-update-superuser")
        location, _ = self._create_location_tree()
        asset = self._create_asset("POST-APPROVE-UPDATE-001", location)
        change_request = self._update_request(requester, asset, proposed_name="Post Admin Approved")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Post Admin Approved")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_manager_can_approve_in_scope_update(self):
        requester = User.objects.create_user(username="post-approve-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._manager_with_location("post-approve-scope-manager", allowed_location)
        asset = self._create_asset("POST-APPROVE-SCOPE-001", allowed_location)
        change_request = self._update_request(requester, asset, proposed_name="Post Scoped Approved")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Post Scoped Approved")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)

    def test_manager_cannot_approve_out_of_scope_update(self):
        requester = User.objects.create_user(username="post-approve-outside-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._manager_with_location("post-approve-outside-manager", allowed_location)
        asset = self._create_asset("POST-APPROVE-OUTSIDE-001", outside_location)
        change_request = self._update_request(requester, asset, proposed_name="Post Outside Approved")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 404)
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertNotEqual(asset.name, "Post Outside Approved")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_regular_user_gets_403_for_approve(self):
        requester = User.objects.create_user(username="post-approve-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="post-approve-regular", password="test-pass-123")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_admin_role_without_approver_flag_can_approve(self):
        requester = User.objects.create_user(username="post-approve-admin-requester", password="test-pass-123")
        reviewer = self._admin_user("post-approve-admin")
        change_request = self._create_request(requester, inventory_number="POST-APPROVE-ADMIN-001")
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.reviewed_by, reviewer)

    def test_approve_non_pending_does_not_change_status(self):
        requester = User.objects.create_user(username="post-approve-nonpending-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-nonpending-superuser")
        change_request = self._create_request(
            requester,
            inventory_number="POST-APPROVE-NONPENDING-001",
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="Already rejected",
        )
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertFalse(Asset.objects.filter(inventory_number="POST-APPROVE-NONPENDING-001").exists())

    def test_bulk_approve_approves_multiple_pending_changes(self):
        requester = User.objects.create_user(username="bulk-approve-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-approve-superuser")
        first_request = self._create_request(requester, inventory_number="BULK-APPROVE-001")
        second_request = self._create_request(requester, inventory_number="BULK-APPROVE-002")
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-approve"),
            data=json.dumps({"ids": [first_request.pk, second_request.pk]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"approved_count": 2, "skipped_count": 0})
        first_request.refresh_from_db()
        second_request.refresh_from_db()
        self.assertEqual(first_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(second_request.status, AssetChangeRequest.Status.APPROVED)

    def test_bulk_approve_skips_out_of_scope_changes(self):
        requester = User.objects.create_user(username="bulk-approve-scope-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._manager_with_location("bulk-approve-scope-manager", allowed_location)
        in_scope_asset = self._create_asset("BULK-APPROVE-IN-SCOPE", allowed_location)
        out_of_scope_asset = self._create_asset("BULK-APPROVE-OUT-SCOPE", outside_location)
        in_scope_request = self._update_request(requester, in_scope_asset, proposed_name="Bulk In Scope Approved")
        out_of_scope_request = self._update_request(requester, out_of_scope_asset, proposed_name="Bulk Out Scope Skipped")
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-approve"),
            data=json.dumps({"ids": [in_scope_request.pk, out_of_scope_request.pk]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"approved_count": 1, "skipped_count": 1})
        in_scope_request.refresh_from_db()
        out_of_scope_request.refresh_from_db()
        self.assertEqual(in_scope_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(out_of_scope_request.status, AssetChangeRequest.Status.PENDING)

    def test_bulk_approve_skips_non_pending_changes(self):
        requester = User.objects.create_user(username="bulk-approve-nonpending-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-approve-nonpending-superuser")
        pending_request = self._create_request(requester, inventory_number="BULK-APPROVE-PENDING")
        approved_request = self._create_request(
            requester,
            inventory_number="BULK-APPROVE-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-approve"),
            data=json.dumps({"ids": [pending_request.pk, approved_request.pk]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"approved_count": 1, "skipped_count": 1})
        pending_request.refresh_from_db()
        approved_request.refresh_from_db()
        self.assertEqual(pending_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(approved_request.status, AssetChangeRequest.Status.APPROVED)

    def test_bulk_approve_regular_user_gets_403(self):
        requester = User.objects.create_user(username="bulk-approve-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="bulk-approve-regular", password="test-pass-123")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-approve"),
            data=json.dumps({"ids": [change_request.pk]}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_bulk_reject_rejects_multiple_pending_changes_with_comment(self):
        requester = User.objects.create_user(username="bulk-reject-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-reject-superuser")
        first_request = self._create_request(requester, inventory_number="BULK-REJECT-001")
        second_request = self._create_request(requester, inventory_number="BULK-REJECT-002")
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-reject"),
            data=json.dumps({"ids": [first_request.pk, second_request.pk], "comment": "Brakuje danych"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rejected": 2, "skipped": 0})
        first_request.refresh_from_db()
        second_request.refresh_from_db()
        self.assertEqual(first_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(second_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(first_request.review_comment, "Brakuje danych")
        self.assertEqual(second_request.review_comment, "Brakuje danych")
        self.assertEqual(first_request.reviewed_by, reviewer)
        self.assertEqual(second_request.reviewed_by, reviewer)
        self.assertIsNotNone(first_request.reviewed_at)
        self.assertIsNotNone(second_request.reviewed_at)

    def test_bulk_reject_empty_comment_returns_400_without_rejecting(self):
        requester = User.objects.create_user(username="bulk-reject-empty-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-reject-empty-superuser")
        change_request = self._create_request(requester, inventory_number="BULK-REJECT-EMPTY")
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-reject"),
            data=json.dumps({"ids": [change_request.pk], "comment": "   "}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "comment is required"})
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.review_comment, "")
        self.assertIsNone(change_request.reviewed_by)
        self.assertIsNone(change_request.reviewed_at)

    def test_bulk_reject_skips_out_of_scope_changes(self):
        requester = User.objects.create_user(username="bulk-reject-scope-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._manager_with_location("bulk-reject-scope-manager", allowed_location)
        in_scope_asset = self._create_asset("BULK-REJECT-IN-SCOPE", allowed_location)
        out_of_scope_asset = self._create_asset("BULK-REJECT-OUT-SCOPE", outside_location)
        in_scope_request = self._update_request(requester, in_scope_asset, proposed_name="Bulk In Scope Rejected")
        out_of_scope_request = self._update_request(requester, out_of_scope_asset, proposed_name="Bulk Out Scope Skipped")
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-reject"),
            data=json.dumps({"ids": [in_scope_request.pk, out_of_scope_request.pk], "comment": "Poza zakresem pomijane"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rejected": 1, "skipped": 1})
        in_scope_request.refresh_from_db()
        out_of_scope_request.refresh_from_db()
        self.assertEqual(in_scope_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(in_scope_request.review_comment, "Poza zakresem pomijane")
        self.assertEqual(out_of_scope_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(out_of_scope_request.review_comment, "")

    def test_bulk_reject_skips_non_pending_changes(self):
        requester = User.objects.create_user(username="bulk-reject-nonpending-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-reject-nonpending-superuser")
        pending_request = self._create_request(requester, inventory_number="BULK-REJECT-PENDING")
        approved_request = self._create_request(
            requester,
            inventory_number="BULK-REJECT-APPROVED",
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
            review_comment="Already approved",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-reject"),
            data=json.dumps({"ids": [pending_request.pk, approved_request.pk], "comment": "Odrzucenie pending"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rejected": 1, "skipped": 1})
        pending_request.refresh_from_db()
        approved_request.refresh_from_db()
        self.assertEqual(pending_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(pending_request.review_comment, "Odrzucenie pending")
        self.assertEqual(approved_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(approved_request.review_comment, "Already approved")

    def test_bulk_reject_regular_user_gets_403(self):
        requester = User.objects.create_user(username="bulk-reject-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="bulk-reject-regular", password="test-pass-123")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:bulk-reject"),
            data=json.dumps({"ids": [change_request.pk], "comment": "Should not save"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.review_comment, "")

    def test_bulk_approve_continues_loop_and_returns_partial_result_on_permission_denied(self):
        from unittest.mock import patch

        requester = User.objects.create_user(username="bulk-approve-pd-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-approve-pd-reviewer")
        first_request = self._create_request(requester, inventory_number="BULK-APPROVE-PD-001")
        second_request = self._create_request(requester, inventory_number="BULK-APPROVE-PD-002")
        self.client.force_login(reviewer)

        call_count = [0]

        def mock_approve(change_request, reviewer):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionDenied("simulated race condition")

        with patch("assets.views.approve_asset_change_request", side_effect=mock_approve):
            response = self.client.post(
                reverse("assets:bulk-approve"),
                data=json.dumps({"ids": [first_request.pk, second_request.pk]}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["approved_count"], 1)
        self.assertEqual(data["skipped_count"], 1)
        self.assertEqual(call_count[0], 2)

    def test_bulk_reject_continues_loop_and_returns_partial_result_on_permission_denied(self):
        from unittest.mock import patch

        requester = User.objects.create_user(username="bulk-reject-pd-requester", password="test-pass-123")
        reviewer = self._superuser("bulk-reject-pd-reviewer")
        first_request = self._create_request(requester, inventory_number="BULK-REJECT-PD-001")
        second_request = self._create_request(requester, inventory_number="BULK-REJECT-PD-002")
        self.client.force_login(reviewer)

        call_count = [0]

        def mock_reject(change_request, reviewer, comment=""):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionDenied("simulated race condition")

        with patch("assets.views.reject_asset_change_request", side_effect=mock_reject):
            response = self.client.post(
                reverse("assets:bulk-reject"),
                data=json.dumps({"ids": [first_request.pk, second_request.pk], "comment": "Odrzucono"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["rejected"], 1)
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(call_count[0], 2)

    def test_superuser_can_reject_with_comment(self):
        requester = User.objects.create_user(username="post-reject-admin-requester", password="test-pass-123")
        reviewer = self._superuser("post-reject-superuser")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Needs more data"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertEqual(change_request.review_comment, "Needs more data")

    def test_manager_can_reject_in_scope_update(self):
        requester = User.objects.create_user(username="post-reject-scope-requester", password="test-pass-123")
        allowed_location, _ = self._create_location_tree()
        reviewer = self._manager_with_location("post-reject-scope-manager", allowed_location)
        asset = self._create_asset("POST-REJECT-SCOPE-001", allowed_location)
        change_request = self._update_request(requester, asset)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Rejected in scope"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        asset.refresh_from_db()
        change_request.refresh_from_db()
        self.assertEqual(asset.name, "Post Asset POST-REJECT-SCOPE-001")
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.review_comment, "Rejected in scope")

    def test_manager_cannot_reject_out_of_scope_update(self):
        requester = User.objects.create_user(username="post-reject-outside-requester", password="test-pass-123")
        allowed_location, outside_location = self._create_location_tree()
        reviewer = self._manager_with_location("post-reject-outside-manager", allowed_location)
        asset = self._create_asset("POST-REJECT-OUTSIDE-001", outside_location)
        change_request = self._update_request(requester, asset)
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Should not save"},
        )

        self.assertEqual(response.status_code, 404)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.review_comment, "")

    def test_regular_user_gets_403_for_reject(self):
        requester = User.objects.create_user(username="post-reject-regular-requester", password="test-pass-123")
        reviewer = User.objects.create_user(username="post-reject-regular", password="test-pass-123")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-reject", kwargs={"pk": change_request.pk}))

        self.assertEqual(response.status_code, 403)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)

    def test_admin_role_without_approver_flag_can_reject(self):
        requester = User.objects.create_user(username="post-reject-admin-requester", password="test-pass-123")
        reviewer = self._admin_user("post-reject-admin")
        change_request = self._create_request(requester)
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-reject", kwargs={"pk": change_request.pk}), {"comment": "Admin reject"})

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertEqual(change_request.reviewed_by, reviewer)
        self.assertEqual(change_request.review_comment, "Admin reject")

    def test_reject_non_pending_does_not_change_status(self):
        requester = User.objects.create_user(username="post-reject-nonpending-requester", password="test-pass-123")
        reviewer = self._superuser("post-reject-nonpending-superuser")
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
            review_comment="Already approved",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Do not overwrite"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.review_comment, "Already approved")

    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_error_message_renders_with_error_css_class(self):
        requester = User.objects.create_user(username="msg-class-error-requester", password="test-pass-123")
        reviewer = self._superuser("msg-class-error-reviewer")
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="Already rejected",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-approve", kwargs={"pk": change_request.pk}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="error"')
        self.assertContains(response, "Nie udało się zatwierdzić wniosku.")

    def test_approve_view_shows_error_message_when_request_is_not_pending(self):
        requester = User.objects.create_user(username="post-approve-stale-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-stale-reviewer")
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.REJECTED,
            review_comment="Already rejected",
        )
        self.client.force_login(reviewer)

        response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.REJECTED)
        self.assertFalse(Asset.objects.filter(inventory_number="POST-CREATE-001").exists())
        self.assertIn("Nie udało się zatwierdzić wniosku.", self._messages(response))

    def test_reject_view_shows_error_message_when_request_is_not_pending(self):
        requester = User.objects.create_user(username="post-reject-stale-requester", password="test-pass-123")
        reviewer = self._superuser("post-reject-stale-reviewer")
        change_request = self._create_request(
            requester,
            status=AssetChangeRequest.Status.APPROVED,
            reviewed_by=reviewer,
            review_comment="Already approved",
        )
        self.client.force_login(reviewer)

        response = self.client.post(
            reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
            {"comment": "Attempt to re-reject"},
        )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, AssetChangeRequest.Status.APPROVED)
        self.assertEqual(change_request.review_comment, "Already approved")
        self.assertIn("Nie udało się odrzucić wniosku.", self._messages(response))

    def test_approve_view_shows_error_message_when_service_raises_permission_denied(self):
        from unittest.mock import patch

        requester = User.objects.create_user(username="post-approve-pd-view-requester", password="test-pass-123")
        reviewer = self._superuser("post-approve-pd-view-reviewer")
        change_request = self._create_request(requester, inventory_number="POST-APPROVE-PD-VIEW-001")
        self.client.force_login(reviewer)

        with patch("assets.views.approve_asset_change_request", side_effect=PermissionDenied("race condition")):
            response = self.client.post(reverse("assets:change-approve", kwargs={"pk": change_request.pk}))

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        self.assertIn("Nie udało się zatwierdzić wniosku.", self._messages(response))

    def test_reject_view_shows_error_message_when_service_raises_permission_denied(self):
        from unittest.mock import patch

        requester = User.objects.create_user(username="post-reject-pd-view-requester", password="test-pass-123")
        reviewer = self._superuser("post-reject-pd-view-reviewer")
        change_request = self._create_request(requester, inventory_number="POST-REJECT-PD-VIEW-001")
        self.client.force_login(reviewer)

        with patch("assets.views.reject_asset_change_request", side_effect=PermissionDenied("race condition")):
            response = self.client.post(
                reverse("assets:change-reject", kwargs={"pk": change_request.pk}),
                {"comment": "Odrzucono"},
            )

        self.assertRedirects(response, reverse("assets:change-detail", kwargs={"pk": change_request.pk}))
        self.assertIn("Nie udało się odrzucić wniosku.", self._messages(response))


class AssetListApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="api-admin",
            email="api-admin@example.com",
            password="test-pass-123",
        )
        for index in range(1, 61):
            Asset.objects.create(
                name=f"Asset {index:03d}",
                inventory_number=f"INV-{index:03d}",
                status=Asset.Status.INACTIVE if index % 2 else Asset.Status.ACTIVE,
                location="HQ" if index <= 30 else "Branch",
                category="IT" if index % 3 else "Furniture",
                purchase_value=Decimal(index * 100),
            )

        cls.target = Asset.objects.create(
            name="Laptop Executive",
            inventory_number="VIP-001",
            status=Asset.Status.INACTIVE,
            location="Board Room",
            category="IT",
            purchase_value=Decimal("9999.99"),
            purchase_date=date(2024, 6, 15),
        )

        cls.date_outside = Asset.objects.create(
            name="Archive Router",
            inventory_number="ARC-001",
            status=Asset.Status.INACTIVE,
            location="Archive",
            category="IT",
            purchase_value=Decimal("2500"),
            purchase_date=date(2023, 3, 10),
        )
        cls.inactive_asset = Asset.objects.create(
            name="Inactive Asset",
            inventory_number="ARCHIVE-API-001",
            status=Asset.Status.LIQUIDATED,
            location="Archive",
            category="IT",
            is_active=False,
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    def test_api_uses_default_pagination(self):
        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertSetEqual(set(payload.keys()), {"results", "pagination", "filters"})
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
                "status": Asset.Status.INACTIVE,
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
                "filter__status__equals": Asset.Status.INACTIVE,
                "filter__purchase_value__gt": "5000",
                "filter__name__contains": "Laptop",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")

    def test_api_returns_runtime_status_for_asset_in_active_inventory(self):
        session = InventorySession.objects.create(number="RINV0001", created_by=self.admin_user)
        location = Location.objects.create(name="Runtime Inventory API")
        InventorySnapshotItem.objects.create(
            session=session,
            asset=self.target,
            asset_id_snapshot=self.target.pk,
            inventory_number=self.target.inventory_number,
            name=self.target.name,
            location_fk_id_snapshot=location.pk,
        )

        response = self.client.get(reverse("assets:api-list"), {"search": self.target.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["status"], Asset.Status.INACTIVE)
        self.assertEqual(row["status_display"], "Nieaktywny")
        self.assertTrue(row["is_in_active_inventory"])
        self.assertEqual(row["runtime_status"], "inventory")
        self.assertEqual(row["runtime_status_display"], "Inwentaryzowany")

    def test_api_does_not_set_runtime_status_for_closed_inventory_session(self):
        session = InventorySession.objects.create(
            number="RINV0002",
            status=InventorySession.Status.CLOSED,
            created_by=self.admin_user,
        )
        location = Location.objects.create(name="Runtime Closed API")
        InventorySnapshotItem.objects.create(
            session=session,
            asset=self.target,
            asset_id_snapshot=self.target.pk,
            inventory_number=self.target.inventory_number,
            name=self.target.name,
            location_fk_id_snapshot=location.pk,
        )

        response = self.client.get(reverse("assets:api-list"), {"search": self.target.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertFalse(row["is_in_active_inventory"])
        self.assertEqual(row["runtime_status"], "")
        self.assertEqual(row["runtime_status_display"], "")

    def test_api_does_not_set_runtime_status_for_asset_outside_active_inventory(self):
        response = self.client.get(reverse("assets:api-list"), {"search": self.target.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertFalse(row["is_in_active_inventory"])
        self.assertEqual(row["runtime_status"], "")
        self.assertEqual(row["runtime_status_display"], "")

    def test_api_filters_by_active_inventory_runtime_state(self):
        location = Location.objects.create(name="Runtime Filter Location")
        in_inventory = Asset.objects.create(
            name="Runtime Filter In",
            inventory_number="RUNTIME-FILTER-IN-001",
            status=Asset.Status.ACTIVE,
            location="Runtime Filter",
        )
        outside_inventory = Asset.objects.create(
            name="Runtime Filter Out",
            inventory_number="RUNTIME-FILTER-OUT-001",
            status=Asset.Status.ACTIVE,
            location="Runtime Filter",
        )
        session = InventorySession.objects.create(number="RINV0003", created_by=self.admin_user)
        InventorySnapshotItem.objects.create(
            session=session,
            asset=in_inventory,
            asset_id_snapshot=in_inventory.pk,
            inventory_number=in_inventory.inventory_number,
            name=in_inventory.name,
            location_fk_id_snapshot=location.pk,
        )

        true_response = self.client.get(
            reverse("assets:api-list"),
            {"location": "Runtime Filter", "filter__is_in_active_inventory__equals": "true"},
        )
        false_response = self.client.get(
            reverse("assets:api-list"),
            {"location": "Runtime Filter", "filter__is_in_active_inventory__equals": "false"},
        )

        self.assertEqual(true_response.status_code, 200)
        self.assertEqual(false_response.status_code, 200)
        self.assertEqual(true_response.json()["pagination"]["total_items"], 1)
        self.assertEqual(true_response.json()["results"][0]["id"], in_inventory.id)
        self.assertEqual(false_response.json()["pagination"]["total_items"], 1)
        self.assertEqual(false_response.json()["results"][0]["id"], outside_inventory.id)

    def test_api_defaults_to_active_scope(self):
        response = self.client.get(reverse("assets:api-list"), {"search": "ARCHIVE-API-001"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagination"]["total_items"], 0)

    def test_api_active_scope_returns_only_active_assets(self):
        response = self.client.get(reverse("assets:api-list"), {"asset_scope": "active", "page_size": 200})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["results"])
        self.assertTrue(all(row["is_active"] for row in response.json()["results"]))

    def test_api_archive_scope_returns_only_inactive_assets(self):
        response = self.client.get(reverse("assets:api-list"), {"asset_scope": "archive"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "ARCHIVE-API-001")
        self.assertFalse(payload["results"][0]["is_active"])

    def test_is_active_filter_does_not_break_active_scope(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {
                "asset_scope": "active",
                "filter__is_active__equals": "false",
                "page_size": 200,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagination"]["total_items"], 0)

    def test_api_filters_by_asset_type_equals(self):
        fixed_asset = Asset.objects.create(
            name="Fixed Filter Asset",
            inventory_number="FILTER-FIXED-001",
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.ACTIVE,
            location="Filter Lab",
        )
        Asset.objects.create(
            name="Low Value Filter Asset",
            inventory_number="FILTER-LOW-001",
            asset_type=Asset.AssetType.LOW_VALUE,
            status=Asset.Status.ACTIVE,
            location="Filter Lab",
        )

        response = self.client.get(
            reverse("assets:api-list"),
            {
                "filter__asset_type__equals": Asset.AssetType.FIXED,
                "location": "Filter Lab",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], fixed_asset.id)

    def test_api_returns_current_quantity_without_legacy_quantity_fields(self):
        asset = Asset.objects.create(
            name="Current Quantity API Asset",
            inventory_number="RQ-API-001",
            record_quantity=37,
            current_quantity=41,
            status=Asset.Status.ACTIVE,
            location="Record Quantity Lab",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["inventory_number"], "RQ-API-001")
        self.assertEqual(row["current_quantity"], 41)
        self.assertNotIn("record_quantity", row)
        self.assertNotIn("last_inventory_quantity", row)

    def test_api_returns_last_inventory_result_fields(self):
        session = InventorySession.objects.create(
            number="INV-API-0001",
            created_by=self.admin_user,
        )
        applied_at = timezone.now()
        asset = Asset.objects.create(
            name="Last Inventory API Asset",
            inventory_number="LI-API-001",
            record_quantity=8,
            current_quantity=5,
            last_inventory_quantity=5,
            last_inventory_session=session,
            last_inventory_at=applied_at,
            status=Asset.Status.ACTIVE,
            location="Last Inventory Lab",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["current_quantity"], 5)
        self.assertEqual(row["last_inventory_session_id"], session.id)
        self.assertEqual(row["last_inventory_session_number"], "INV-API-0001")
        self.assertEqual(row["last_inventory_at"], applied_at.isoformat())
        self.assertEqual(row["last_inventory_at_display"], applied_at.strftime("%Y-%m-%d %H:%M"))

    def test_api_returns_zero_current_quantity_as_zero(self):
        asset = Asset.objects.create(
            name="Zero Last Inventory API Asset",
            inventory_number="LI-ZERO-API-001",
            record_quantity=3,
            current_quantity=0,
            last_inventory_quantity=0,
            status=Asset.Status.ACTIVE,
            location="Last Inventory Lab",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["current_quantity"], 0)

    def test_api_handles_asset_without_applied_inventory_result(self):
        asset = Asset.objects.create(
            name="No Last Inventory API Asset",
            inventory_number="LI-NONE-API-001",
            status=Asset.Status.ACTIVE,
            location="Last Inventory Lab",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["current_quantity"], 1)
        self.assertEqual(row["last_inventory_at"], "")
        self.assertEqual(row["last_inventory_at_display"], "")
        self.assertIsNone(row["last_inventory_session_id"])
        self.assertEqual(row["last_inventory_session_number"], "")

    def test_api_displays_custom_dictionary_asset_type_name(self):
        AssetTypeDictionary.objects.create(
            name="Testowy Ilościowy",
            code="tt",
            is_quantity_based=True,
            is_active=True,
            sort_order=70,
        )
        asset = Asset.objects.create(
            name="Custom Type Display Asset",
            inventory_number="DISPLAY-CUSTOM-001",
            asset_type="tt",
            status=Asset.Status.ACTIVE,
            location="Display Lab",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["asset_type"], "tt")
        self.assertEqual(row["asset_type_display"], "Testowy Ilościowy")

    def test_api_displays_legacy_asset_type_name(self):
        asset = Asset.objects.create(
            name="Legacy Type Display Asset",
            inventory_number="DISPLAY-LEGACY-001",
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.ACTIVE,
            location="Display Lab",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["asset_type"], "fixed")
        self.assertEqual(row["asset_type_display"], "Środek trwały")

    def test_api_uses_dictionary_name_when_asset_type_ref_is_null(self):
        AssetTypeDictionary.objects.create(
            name="Słownik bez FK",
            code="no-ref-type",
            is_active=True,
            sort_order=80,
        )
        asset = Asset.objects.create(
            name="No Ref Type Display Asset",
            inventory_number="DISPLAY-NO-REF-001",
            asset_type="no-ref-type",
            status=Asset.Status.ACTIVE,
            location="Display Lab",
        )
        Asset.objects.filter(pk=asset.pk).update(asset_type_ref=None)

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertEqual(row["asset_type"], "no-ref-type")
        self.assertEqual(row["asset_type_display"], "Słownik bez FK")

    def test_asset_type_filter_schema_uses_current_model_choices(self):
        schema = get_asset_filter_ui_schema()
        asset_type_schema = next(item for item in schema if item["field"] == "asset_type")

        self.assertEqual(asset_type_schema["label"], "Rodzaj")
        self.assertEqual(
            asset_type_schema["choices"],
            [
                {"value": "fixed", "label": "Środek trwały"},
                {"value": "low_value", "label": "Wyposażenie / niskocenne"},
                {"value": "intangible", "label": "WNiP"},
                {"value": "quantity", "label": "Ilościówka"},
                {"value": "other", "label": "Inne"},
            ],
        )
        self.assertFalse(
            {"fixed_asset", "low_value_asset", "it_equipment"}.intersection(
                {choice["value"] for choice in asset_type_schema["choices"]}
            )
        )

    def test_runtime_inventory_filter_schema_is_separate_from_status(self):
        schema = get_asset_filter_ui_schema()
        runtime_schema = next(item for item in schema if item["field"] == "is_in_active_inventory")
        status_schema = next(item for item in schema if item["field"] == "status")

        self.assertEqual(runtime_schema["label"], "W aktywnej inwentaryzacji")
        self.assertEqual(
            runtime_schema["choices"],
            [{"value": "true", "label": "Tak"}, {"value": "false", "label": "Nie"}],
        )
        self.assertFalse(any(choice["value"] == "inventory" for choice in status_schema["choices"]))

    def test_api_supports_enum_in_filters(self):
        response = self.client.get(
            reverse("assets:api-list"),
            {"filter__status__in": ",".join([Asset.Status.ACTIVE, Asset.Status.INACTIVE])},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 62)

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
                "filter__status__equals": Asset.Status.INACTIVE,
                "filter__name__contains": "Laptop",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["inventory_number"], "VIP-001")


class AssetExportCsvApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Eksport Warszawa")
        cls.child_location = Location.objects.create(name="Magazyn", parent=cls.root_location)
        cls.other_location = Location.objects.create(name="Eksport Krakow")
        cls.admin_user = User.objects.create_superuser(
            username="export-admin",
            email="export-admin@example.com",
            password="test-pass-123",
        )
        cls.manager_user = User.objects.create_user(username="export-manager", password="test-pass-123")
        cls.manager_user.profile.role = UserProfile.Role.MANAGER
        cls.manager_user.profile.save(update_fields=["role"])
        cls.manager_user.profile.allowed_locations.add(cls.root_location)

        Asset.objects.create(
            name="Za\u017c\u00f3\u0142\u0107 laptop",
            inventory_number="EXP-001",
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.ACTIVE,
            location_fk=cls.root_location,
            category="IT",
            purchase_value=Decimal("1234.50"),
            record_quantity=7,
            current_quantity=3,
            last_inventory_quantity=3,
            purchase_date=date(2024, 5, 1),
        )
        Asset.objects.create(
            name="Export Monitor",
            inventory_number="EXP-002",
            asset_type=Asset.AssetType.LOW_VALUE,
            status=Asset.Status.INACTIVE,
            location_fk=cls.child_location,
            category="IT",
            purchase_value=Decimal("2500.00"),
            record_quantity=4,
            current_quantity=4,
        )
        Asset.objects.create(
            name="Outside Export",
            inventory_number="EXP-003",
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.ACTIVE,
            location_fk=cls.other_location,
            category="Office",
            purchase_value=Decimal("500.00"),
        )
        Asset.objects.create(
            name="Archived Export",
            inventory_number="EXP-ARCHIVE-001",
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.LIQUIDATED,
            location_fk=cls.root_location,
            category="IT",
            is_active=False,
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    def _export(self, params=None):
        return self.client.get(reverse("assets:api-export"), params or {})

    def _csv_rows(self, response):
        text = response.content.decode("utf-8-sig")
        return list(csv.reader(StringIO(text), delimiter=";"))

    def test_export_requires_login(self):
        self.client.logout()

        response = self._export({"columns": "inventory_number"})

        self.assertEqual(response.status_code, 302)

    def test_export_respects_user_location_scope(self):
        self.client.force_login(self.manager_user)

        response = self._export({"columns": "inventory_number,name", "ordering": "inventory_number"})

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual([row[0] for row in rows[1:]], ["EXP-001", "EXP-002"])

    def test_export_defaults_to_active_scope(self):
        response = self._export({"columns": "inventory_number", "ordering": "inventory_number"})

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        inventory_numbers = [row[0] for row in rows[1:]]
        self.assertIn("EXP-001", inventory_numbers)
        self.assertNotIn("EXP-ARCHIVE-001", inventory_numbers)

    def test_export_archive_scope_exports_inactive_assets(self):
        response = self._export(
            {
                "asset_scope": "archive",
                "columns": "inventory_number,status",
                "ordering": "inventory_number",
            }
        )

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual(rows, [["Nr inwentarzowy", "Status"], ["EXP-ARCHIVE-001", "Zlikwidowany"]])

    def test_export_respects_search(self):
        response = self._export({"columns": "inventory_number,name", "search": "Monitor"})

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual(rows, [["Nr inwentarzowy", "Nazwa"], ["EXP-002", "Export Monitor"]])

    def test_export_respects_dynamic_filters(self):
        response = self._export(
            {
                "columns": "inventory_number,status",
                "filter__status__equals": Asset.Status.INACTIVE,
            }
        )

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual(rows, [["Nr inwentarzowy", "Status"], ["EXP-002", "Nieaktywny"]])

    def test_export_respects_ordering(self):
        response = self._export({"columns": "inventory_number", "ordering": "-purchase_value"})

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual([row[0] for row in rows[1:]], ["EXP-002", "EXP-001", "EXP-003"])

    def test_export_respects_columns_and_order(self):
        response = self._export({"columns": "name,inventory_number,purchase_value", "search": "EXP-001"})

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual(rows[0], ["Nazwa", "Nr inwentarzowy", "Wartość"])
        self.assertEqual(rows[1], ["Za\u017c\u00f3\u0142\u0107 laptop", "EXP-001", "1234.50"])

    def test_export_ignores_blocked_columns(self):
        response = self._export(
            {
                "columns": "select,inventory_number,record_quantity,last_inventory_quantity,terminal,odczyt,reczne,ilosc_faktyczna,name",
                "search": "EXP-001",
            }
        )

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual(rows[0], ["Nr inwentarzowy", "Nazwa"])
        self.assertEqual(rows[1], ["EXP-001", "Za\u017c\u00f3\u0142\u0107 laptop"])

    def test_export_current_quantity_matches_asset_list_logic(self):
        response = self._export({"columns": "inventory_number,current_quantity", "ordering": "inventory_number"})

        self.assertEqual(response.status_code, 200)
        rows = self._csv_rows(response)
        self.assertEqual(rows[0], ["Nr inwentarzowy", "Ilość"])
        self.assertEqual(rows[1], ["EXP-001", "3"])
        self.assertEqual(rows[2], ["EXP-002", "4"])

    def test_export_csv_contains_polish_characters(self):
        response = self._export({"columns": "name", "search": "EXP-001"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("Za\u017c\u00f3\u0142\u0107", response.content.decode("utf-8-sig"))

    def test_export_content_type_is_csv_utf8(self):
        response = self._export({"columns": "inventory_number"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertRegex(
            response["Content-Disposition"],
            r'attachment; filename="assets-export-\d{4}-\d{2}-\d{2}\.csv"',
        )

    def test_export_without_columns_returns_400(self):
        response = self._export()

        self.assertEqual(response.status_code, 400)

    def test_export_with_only_blocked_columns_returns_400(self):
        response = self._export(
            {"columns": "select,record_quantity,last_inventory_quantity,terminal,odczyt,reczne,ilosc_faktyczna"}
        )

        self.assertEqual(response.status_code, 400)


class AssetListApiExtendedTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="api-extended-admin",
            email="api-extended-admin@example.com",
            password="test-pass-123",
        )
        self.client.force_login(self.admin_user)

    def test_api_exposes_extended_system_columns(self):
        user = User.objects.create_user(username="operator", first_name="Jan", last_name="Kowalski")
        asset = Asset.objects.create(
            name="Laptop Test",
            inventory_number="EXT-001",
            asset_type=Asset.AssetType.LOW_VALUE,
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
        self.assertEqual(row["asset_type_display"], "Wyposażenie / niskocenne")
        self.assertEqual(row["manufacturer"], "Dell")
        self.assertEqual(row["serial_number"], "SN-123")
        self.assertEqual(row["responsible_person"], "Jan Kowalski")
        self.assertEqual(row["current_user"], "Jan Kowalski")
        self.assertEqual(row["technical_condition_display"], "Bardzo dobry")
        self.assertEqual(row["invoice_number"], "FV/2026/001")
        self.assertEqual(row["external_id"], "ERP-001")
        self.assertEqual(row["cost_center"], "MPK-01")
        self.assertEqual(row["is_active_display"], "Tak")

    def test_api_marks_asset_with_pending_update(self):
        requester = User.objects.create_user(username="pending-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Pending Update Asset",
            inventory_number="PENDING-UPD-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Pending Update Asset Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_pending_update"])

    def test_api_marks_asset_with_rejected_update(self):
        requester = User.objects.create_user(username="rejected-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Rejected Update Asset",
            inventory_number="REJECTED-UPD-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.REJECTED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Rejected Update Asset Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_rejected_update"])
        self.assertEqual(row["rejected_update_comment"], "")

    def test_api_exposes_rejected_update_comment_for_requesting_user(self):
        requester = User.objects.create_user(username="rejected-comment-requester", password="test-pass-123")
        location = Location.objects.create(name="Rejected Comment Location")
        requester.profile.allowed_locations.add(location)
        self.client.force_login(requester)
        asset = Asset.objects.create(
            name="Rejected Comment Asset",
            inventory_number="REJECTED-COMMENT-001",
            status=Asset.Status.ACTIVE,
            location=location.path,
            location_fk=location,
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.REJECTED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Rejected Comment Edited"}},
            review_comment="Brakuje numeru seryjnego",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_rejected_update"])
        self.assertEqual(row["rejected_update_comment"], "Brakuje numeru seryjnego")

    def test_api_does_not_expose_other_users_rejected_update_comment_to_regular_user(self):
        requester = User.objects.create_user(username="rejected-comment-owner", password="test-pass-123")
        viewer = User.objects.create_user(username="rejected-comment-viewer", password="test-pass-123")
        location = Location.objects.create(name="Other Rejected Comment Location")
        viewer.profile.allowed_locations.add(location)
        self.client.force_login(viewer)
        asset = Asset.objects.create(
            name="Other Rejected Comment Asset",
            inventory_number="REJECTED-COMMENT-OTHER",
            status=Asset.Status.ACTIVE,
            location=location.path,
            location_fk=location,
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.REJECTED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Other Rejected Comment Edited"}},
            review_comment="Komentarz innego użytkownika",
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_rejected_update"])
        self.assertEqual(row["rejected_update_comment"], "")

    def test_api_does_not_mark_asset_without_pending_update(self):
        requester = User.objects.create_user(username="approved-update-requester", password="test-pass-123")
        pending_asset = Asset.objects.create(
            name="Other Pending Update Asset",
            inventory_number="PENDING-UPD-OTHER",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        asset = Asset.objects.create(
            name="No Pending Update Asset",
            inventory_number="NO-PENDING-UPD-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=pending_asset,
            payload={"current": {"name": pending_asset.name}, "proposed": {"name": "Other Edited"}},
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.APPROVED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Approved Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertFalse(row["has_pending_update"])
        self.assertFalse(row["has_rejected_update"])
        self.assertEqual(row["rejected_update_comment"], "")

    def test_api_approved_update_does_not_mark_asset_as_rejected(self):
        requester = User.objects.create_user(username="approved-rejected-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Approved Update Asset",
            inventory_number="APPROVED-UPD-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.APPROVED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Approved Update Asset Edited"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertFalse(row["has_rejected_update"])

    def test_api_marks_pending_and_rejected_update_flags_independently(self):
        requester = User.objects.create_user(username="pending-rejected-update-requester", password="test-pass-123")
        asset = Asset.objects.create(
            name="Pending And Rejected Update Asset",
            inventory_number="PENDING-REJECTED-UPD-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.REJECTED,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Rejected Name"}},
        )
        AssetChangeRequest.objects.create(
            requested_by=requester,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=AssetChangeRequest.Status.PENDING,
            asset=asset,
            payload={"current": {"name": asset.name}, "proposed": {"name": "Pending Name"}},
        )

        response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})

        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertTrue(row["has_pending_update"])
        self.assertTrue(row["has_rejected_update"])

    def test_api_pending_update_marker_does_not_add_per_row_queries(self):
        requester = User.objects.create_user(username="pending-query-requester", password="test-pass-123")
        assets = [
            Asset.objects.create(
                name=f"Pending Query Asset {index}",
                inventory_number=f"PENDING-QUERY-{index:03d}",
                status=Asset.Status.ACTIVE,
                location="Pending Query",
                category="IT",
            )
            for index in range(3)
        ]
        for asset in assets:
            AssetChangeRequest.objects.create(
                requested_by=requester,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
                asset=asset,
                payload={"current": {"name": asset.name}, "proposed": {"name": f"{asset.name} Edited"}},
            )
            AssetChangeRequest.objects.create(
                requested_by=requester,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.REJECTED,
                asset=asset,
                payload={"current": {"name": asset.name}, "proposed": {"name": f"{asset.name} Rejected"}},
            )

        with self.assertNumQueries(4):
            response = self.client.get(
                reverse("assets:api-list"),
                {"search": "PENDING-QUERY", "page_size": 3},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 3)


class AssetBulkMoveApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_superuser(
            username="bulk-admin",
            email="bulk-admin@example.com",
            password="test-pass-123",
        )
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.target_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.inactive_location = Location.objects.create(name="Archiwum", parent=cls.root_location, is_active=False)
        cls.asset_one = Asset.objects.create(
            name="Bulk Asset 1",
            inventory_number="BULK-001",
            status=Asset.Status.ACTIVE,
            location="HQ",
            location_fk=cls.root_location,
            category="IT",
        )
        cls.asset_two = Asset.objects.create(
            name="Bulk Asset 2",
            inventory_number="BULK-002",
            status=Asset.Status.INACTIVE,
            location="Branch",
            location_fk=cls.root_location,
            category="IT",
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

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

    def test_bulk_move_creates_moved_history_entry(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_one.id],
                "target_location_id": self.target_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        entry = AssetHistoryEntry.objects.get(asset=self.asset_one)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.MOVED)
        self.assertEqual(entry.description, "Przeniesiono środek")
        self.assertEqual(entry.operator, self.admin_user)
        self.assertEqual(entry.field_name, "location_fk")
        self.assertEqual(entry.old_value, self.root_location.path)
        self.assertEqual(entry.new_value, self.target_location.path)

    def test_bulk_move_does_not_create_history_when_location_is_unchanged(self):
        asset = Asset.objects.create(
            name="Bulk Already There",
            inventory_number="BULK-UNCHANGED-001",
            status=Asset.Status.ACTIVE,
            location=self.target_location.path,
            location_fk=self.target_location,
            category="IT",
        )

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [asset.id],
                "target_location_id": self.target_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_bulk_move_rejects_archived_asset(self):
        asset = Asset.objects.create(
            name="Bulk Archived",
            inventory_number="BULK-ARCHIVED-001",
            status=Asset.Status.LIQUIDATED,
            location=self.root_location.path,
            location_fk=self.root_location,
            category="IT",
            is_active=False,
        )

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [asset.id],
                "target_location_id": self.target_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])
        asset.refresh_from_db()
        self.assertEqual(asset.location_fk, self.root_location)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_bulk_move_creates_history_for_each_moved_asset(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_one.id, self.asset_two.id],
                "target_location_id": self.target_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        entries = AssetHistoryEntry.objects.filter(
            asset__in=[self.asset_one, self.asset_two],
            event_type=AssetHistoryEntry.EventType.MOVED,
        )
        self.assertEqual(entries.count(), 2)

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
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("assets:api-bulk-move"))

        self.assertEqual(response.status_code, 405)

    def test_bulk_move_location_not_updated_when_history_fails(self):
        from unittest.mock import patch

        original_location = self.asset_one.location
        original_location_fk = self.asset_one.location_fk

        with patch.object(
            AssetHistoryEntry.objects, "bulk_create", side_effect=RuntimeError("simulated failure")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("assets:api-bulk-move"),
                    data={
                        "asset_ids": [self.asset_one.id],
                        "target_location_id": self.target_location.id,
                    },
                    content_type="application/json",
                )

        self.asset_one.refresh_from_db()
        self.assertEqual(self.asset_one.location, original_location)
        self.assertEqual(self.asset_one.location_fk, original_location_fk)


class AssetBulkMoveApiAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.child_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.grandchild_location = Location.objects.create(name="Pietro 1", parent=cls.child_location)
        cls.other_root_location = Location.objects.create(name="Krakow")
        cls.other_child_location = Location.objects.create(name="Magazyn", parent=cls.other_root_location)

        cls.admin_user = User.objects.create_superuser(
            username="bulk-scope-admin",
            email="bulk-scope-admin@example.com",
            password="test-pass-123",
        )
        cls.manager_user = User.objects.create_user(username="bulk-scope-manager", password="test-pass-123")
        cls.manager_user.profile.role = UserProfile.Role.MANAGER
        cls.manager_user.profile.save(update_fields=["role"])
        cls.manager_user.profile.allowed_locations.add(cls.root_location)

        cls.asset_in_scope = Asset.objects.create(
            name="In Scope",
            inventory_number="BULK-S-001",
            status=Asset.Status.ACTIVE,
            location=cls.child_location.path,
            location_fk=cls.child_location,
            category="IT",
        )
        cls.asset_in_scope_two = Asset.objects.create(
            name="In Scope Two",
            inventory_number="BULK-S-002",
            status=Asset.Status.ACTIVE,
            location=cls.grandchild_location.path,
            location_fk=cls.grandchild_location,
            category="IT",
        )
        cls.asset_out_of_scope = Asset.objects.create(
            name="Out of Scope",
            inventory_number="BULK-S-003",
            status=Asset.Status.ACTIVE,
            location=cls.other_child_location.path,
            location_fk=cls.other_child_location,
            category="IT",
        )
        cls.asset_without_fk = Asset.objects.create(
            name="Without FK",
            inventory_number="BULK-S-004",
            status=Asset.Status.ACTIVE,
            location="Legacy only",
            location_fk=None,
            category="IT",
        )

    def test_admin_can_bulk_move_any_asset_to_any_location(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_out_of_scope.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.asset_out_of_scope.refresh_from_db()
        self.assertEqual(self.asset_out_of_scope.location, self.child_location.path)
        self.assertEqual(self.asset_out_of_scope.location_fk, self.child_location)

    def test_user_can_move_asset_within_allowed_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id],
                "target_location_id": self.grandchild_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.asset_in_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location, self.grandchild_location.path)
        self.assertEqual(self.asset_in_scope.location_fk, self.grandchild_location)
        entry = AssetHistoryEntry.objects.get(asset=self.asset_in_scope)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.MOVED)
        self.assertEqual(entry.operator, self.manager_user)

    def test_user_cannot_move_asset_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_out_of_scope.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_out_of_scope.refresh_from_db()
        self.assertEqual(self.asset_out_of_scope.location_fk, self.other_child_location)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=self.asset_out_of_scope).exists())

    def test_user_cannot_move_asset_to_location_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id],
                "target_location_id": self.other_child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_in_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location_fk, self.child_location)

    def test_user_cannot_move_asset_with_null_location_fk(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_without_fk.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_without_fk.refresh_from_db()
        self.assertIsNone(self.asset_without_fk.location_fk)
        self.assertEqual(self.asset_without_fk.location, "Legacy only")

    def test_unauthorized_request_does_not_partially_move_assets(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id, self.asset_out_of_scope.id],
                "target_location_id": self.grandchild_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        self.asset_in_scope.refresh_from_db()
        self.asset_out_of_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location, self.child_location.path)
        self.assertEqual(self.asset_in_scope.location_fk, self.child_location)
        self.assertEqual(self.asset_out_of_scope.location, self.other_child_location.path)
        self.assertEqual(self.asset_out_of_scope.location_fk, self.other_child_location)

    def test_bulk_move_anonymous_user_redirects_to_login(self):
        response = self.client.post(
            reverse("assets:api-bulk-move"),
            data={
                "asset_ids": [self.asset_in_scope.id],
                "target_location_id": self.child_location.id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))
        self.asset_in_scope.refresh_from_db()
        self.assertEqual(self.asset_in_scope.location_fk, self.child_location)


class AssetBulkWithdrawApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Withdraw Warszawa")
        cls.child_location = Location.objects.create(name="Withdraw Biuro", parent=cls.root_location)
        cls.other_location = Location.objects.create(name="Withdraw Krakow")

        cls.admin_user = User.objects.create_superuser(
            username="bulk-withdraw-admin",
            email="bulk-withdraw-admin@example.com",
            password="test-pass-123",
        )
        cls.manager_user = User.objects.create_user(username="bulk-withdraw-manager", password="test-pass-123")
        cls.manager_user.profile.role = UserProfile.Role.MANAGER
        cls.manager_user.profile.save(update_fields=["role"])
        cls.manager_user.profile.allowed_locations.add(cls.root_location)

        cls.regular_user = User.objects.create_user(username="bulk-withdraw-user", password="test-pass-123")
        cls.regular_user.profile.allowed_locations.add(cls.root_location)

    def _create_asset(self, inventory_number, location=None, **overrides):
        location = location or self.child_location
        defaults = {
            "name": "Bulk Withdraw Asset",
            "inventory_number": inventory_number,
            "status": Asset.Status.ACTIVE,
            "location": location.path,
            "location_fk": location,
            "category": "IT",
            "current_quantity": 7,
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _post_bulk_withdraw(self, asset_ids):
        return self.client.post(
            reverse("assets:api-bulk-withdraw"),
            data={"asset_ids": asset_ids},
            content_type="application/json",
        )

    def test_bulk_withdraw_works(self):
        asset_one = self._create_asset("BULK-WITHDRAW-001")
        asset_two = self._create_asset("BULK-WITHDRAW-002")
        self.client.force_login(self.admin_user)

        response = self._post_bulk_withdraw([asset_one.id, asset_two.id])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["success"], True)
        self.assertEqual(response.json()["updated_count"], 2)
        for asset in (asset_one, asset_two):
            asset.refresh_from_db()
            self.assertFalse(asset.is_active)
            self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
            self.assertEqual(asset.current_quantity, 7)
            self.assertEqual(asset.location_fk, self.child_location)

    def test_manager_can_bulk_withdraw_assets_in_scope(self):
        asset = self._create_asset("BULK-WITHDRAW-SCOPE-001")
        self.client.force_login(self.manager_user)

        response = self._post_bulk_withdraw([asset.id])

        self.assertEqual(response.status_code, 200)
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)

    def test_manager_cannot_bulk_withdraw_assets_outside_scope(self):
        asset = self._create_asset("BULK-WITHDRAW-SCOPE-002", location=self.other_location)
        self.client.force_login(self.manager_user)

        response = self._post_bulk_withdraw([asset.id])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_user_cannot_bulk_withdraw_assets(self):
        asset = self._create_asset("BULK-WITHDRAW-USER-001")
        self.client.force_login(self.regular_user)

        response = self._post_bulk_withdraw([asset.id])

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["success"], False)
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)

    def test_bulk_withdraw_moves_asset_from_active_list_to_archive(self):
        asset = self._create_asset("BULK-WITHDRAW-LISTS-001")
        self.client.force_login(self.manager_user)

        self._post_bulk_withdraw([asset.id])

        active_response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})
        archive_response = self.client.get(
            reverse("assets:api-list"),
            {"asset_scope": "archive", "search": asset.inventory_number},
        )

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_response.json()["pagination"]["total_items"], 0)
        self.assertEqual(archive_response.status_code, 200)
        self.assertEqual(archive_response.json()["pagination"]["total_items"], 1)
        self.assertEqual(archive_response.json()["results"][0]["inventory_number"], asset.inventory_number)

    def test_bulk_withdraw_creates_history_entries(self):
        asset_one = self._create_asset("BULK-WITHDRAW-HISTORY-001")
        asset_two = self._create_asset("BULK-WITHDRAW-HISTORY-002")
        self.client.force_login(self.manager_user)

        response = self._post_bulk_withdraw([asset_one.id, asset_two.id])

        self.assertEqual(response.status_code, 200)
        entries = AssetHistoryEntry.objects.filter(
            asset__in=[asset_one, asset_two],
            event_type=AssetHistoryEntry.EventType.WITHDRAWN,
        )
        self.assertEqual(entries.count(), 2)
        for entry in entries:
            self.assertEqual(entry.description, "Wycofano środek do Archiwum.")
            self.assertEqual(entry.operator, self.manager_user)
            self.assertEqual(entry.field_name, "is_active")
            self.assertEqual(entry.old_value, "Aktywna Ewidencja")
            self.assertEqual(entry.new_value, "Archiwum - Zlikwidowany")

    def test_bulk_withdraw_rejects_archived_asset(self):
        asset = self._create_asset(
            "BULK-WITHDRAW-ARCHIVED-001",
            status=Asset.Status.LIQUIDATED,
            is_active=False,
        )
        self.client.force_login(self.admin_user)

        response = self._post_bulk_withdraw([asset.id])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["success"], False)
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_bulk_withdraw_rejects_get(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("assets:api-bulk-withdraw"))

        self.assertEqual(response.status_code, 405)


class AssetListApiLocationAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root_location = Location.objects.create(name="Warszawa")
        cls.child_location = Location.objects.create(name="Budynek A", parent=cls.root_location)
        cls.grandchild_location = Location.objects.create(name="Pietro 1", parent=cls.child_location)
        cls.other_root_location = Location.objects.create(name="Krakow")
        cls.other_child_location = Location.objects.create(name="Magazyn", parent=cls.other_root_location)

        cls.asset_root = Asset.objects.create(
            name="Asset Root",
            inventory_number="ACL-001",
            status=Asset.Status.ACTIVE,
            location="nieuzywane-root",
            location_fk=cls.root_location,
            category="IT",
        )
        cls.asset_child = Asset.objects.create(
            name="Asset Child",
            inventory_number="ACL-002",
            status=Asset.Status.ACTIVE,
            location="nieuzywane-child",
            location_fk=cls.child_location,
            category="IT",
        )
        cls.asset_grandchild = Asset.objects.create(
            name="Asset Grandchild",
            inventory_number="ACL-003",
            status=Asset.Status.ACTIVE,
            location="nieuzywane-grandchild",
            location_fk=cls.grandchild_location,
            category="IT",
        )
        cls.asset_outside = Asset.objects.create(
            name="Asset Outside",
            inventory_number="ACL-004",
            status=Asset.Status.ACTIVE,
            location="nieuzywane-outside",
            location_fk=cls.other_child_location,
            category="IT",
        )
        cls.asset_without_fk = Asset.objects.create(
            name="Asset Without FK",
            inventory_number="ACL-005",
            status=Asset.Status.ACTIVE,
            location="nieuzywane-null",
            location_fk=None,
            category="IT",
        )

        cls.admin_user = User.objects.create_superuser(
            username="scope-admin",
            email="scope-admin@example.com",
            password="test-pass-123",
        )
        cls.manager_user = User.objects.create_user(username="scope-manager", password="test-pass-123")
        cls.manager_user.profile.role = UserProfile.Role.MANAGER
        cls.manager_user.profile.save(update_fields=["role"])
        cls.manager_user.profile.allowed_locations.add(cls.root_location)

        cls.no_access_user = User.objects.create_user(username="scope-empty", password="test-pass-123")
        cls.no_access_user.profile.role = UserProfile.Role.USER
        cls.no_access_user.profile.save(update_fields=["role"])

    def test_admin_sees_all_assets(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        returned_ids = {row["id"] for row in payload["results"]}
        self.assertEqual(payload["pagination"]["total_items"], 5)
        self.assertSetEqual(
            returned_ids,
            {
                self.asset_root.id,
                self.asset_child.id,
                self.asset_grandchild.id,
                self.asset_outside.id,
                self.asset_without_fk.id,
            },
        )

    def test_user_sees_asset_from_allowed_location(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"), {"search": "ACL-001"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], self.asset_root.id)

    def test_user_sees_asset_from_descendant_location(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"), {"search": "ACL-003"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 1)
        self.assertEqual(payload["results"][0]["id"], self.asset_grandchild.id)

    def test_user_does_not_see_assets_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        returned_ids = {row["id"] for row in payload["results"]}
        self.assertEqual(payload["pagination"]["total_items"], 3)
        self.assertIn(self.asset_root.id, returned_ids)
        self.assertIn(self.asset_child.id, returned_ids)
        self.assertIn(self.asset_grandchild.id, returned_ids)
        self.assertNotIn(self.asset_outside.id, returned_ids)

    def test_user_without_allowed_locations_sees_no_assets(self):
        self.client.force_login(self.no_access_user)

        response = self.client.get(reverse("assets:api-list"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 0)
        self.assertEqual(payload["results"], [])

    def test_user_does_not_see_assets_with_null_location_fk(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("assets:api-list"), {"search": "ACL-005"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["pagination"]["total_items"], 0)
        self.assertEqual(payload["results"], [])


class AssetListViewTests(TestCase):
    def _create_change_request(self, user, status, marker="SUMMARY-REQUEST"):
        return AssetChangeRequest.objects.create(
            requested_by=user,
            operation=AssetChangeRequest.Operation.UPDATE,
            status=status,
            payload={"current": {"name": marker}, "proposed": {"name": f"{marker} updated"}},
        )

    def test_list_view_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_list_view_exposes_filters_for_authenticated_user(self):
        Asset.objects.create(
            name="Monitor",
            inventory_number="MON-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_user(username="viewer", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-api-url="/api/assets/"')
        self.assertContains(response, 'data-export-url="/api/assets/export/"')
        self.assertContains(response, 'id="asset-export-csv"')
        self.assertContains(response, "Eksport CSV")
        self.assertContains(response, '<button id="asset-management-delete" type="button" class="ui-btn ui-btn--danger" hidden disabled>Wycofaj</button>', html=True)
        self.assertContains(response, 'elements.managementDeleteButton.disabled = state.withdrawSubmitting || getSelectedAssetIds().length < 1;')
        self.assertContains(response, 'const selectedAsset = event.target.closest("[data-role=\'asset-select\']");')
        self.assertContains(response, "<option value=\"Warehouse\">Warehouse</option>", html=True)
        self.assertContains(response, "W aktywnej inwentaryzacji")
        self.assertContains(response, "asset-runtime-status-pill")
        self.assertContains(response, "row.is_in_active_inventory")
        self.assertNotContains(response, "asset-status-stack")

    def test_archive_view_renders_with_archive_api_url(self):
        Asset.objects.create(
            name="Archived Monitor",
            inventory_number="MON-ARCHIVE-001",
            status=Asset.Status.LIQUIDATED,
            location="Warehouse",
            category="IT",
            is_active=False,
        )
        user = User.objects.create_user(username="archive-viewer", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:archive"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Archiwum środków")
        self.assertContains(response, "Widok środków wycofanych z aktywnej ewidencji.")
        self.assertContains(response, 'data-api-url="/api/assets/?asset_scope=archive"')
        self.assertContains(response, 'data-asset-list-mode="archive"')
        self.assertContains(response, 'const isArchiveMode = assetListMode === "archive";')
        self.assertContains(response, reverse("assets:list"))

    def test_manager_sees_change_queue_menu(self):
        user = User.objects.create_user(username="viewer-manager-menu", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("assets:change-list")}"')
        self.assertContains(response, "Kolejka zmian")

    def test_regular_user_does_not_see_change_queue_menu(self):
        user = User.objects.create_user(username="viewer-user-menu", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'href="{reverse("assets:change-list")}"')

    def test_list_view_shows_approved_change_summary_for_regular_user(self):
        user = User.objects.create_user(username="viewer-approved-summary", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Status.APPROVED, "SUMMARY-APPROVED-1")
        self._create_change_request(user, AssetChangeRequest.Status.APPROVED, "SUMMARY-APPROVED-2")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="asset-change-summary" class="asset-change-summary"')
        self.assertContains(response, 'data-role="dismiss-summary"')
        self.assertContains(response, "×")
        self.assertContains(response, "Zatwierdzono 2 zmian")
        self.assertContains(response, "Odrzucono 0 zmian")
        self.assertNotContains(response, "Sprawdź oznaczone środki")

    def test_list_view_shows_rejected_change_summary_for_regular_user(self):
        user = User.objects.create_user(username="viewer-rejected-summary", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Status.REJECTED, "SUMMARY-REJECTED-1")
        self._create_change_request(user, AssetChangeRequest.Status.REJECTED, "SUMMARY-REJECTED-2")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zatwierdzono 0 zmian")
        self.assertContains(response, "Odrzucono 2 zmian")
        self.assertNotContains(response, "Sprawdź oznaczone środki")
        self.assertNotContains(response, f'href="{reverse("assets:change-list")}?status=rejected"')
        self.assertContains(response, "Powody znajdziesz przy oznaczonych środkach")

    def test_list_view_hides_change_summary_without_decided_requests(self):
        user = User.objects.create_user(username="viewer-no-summary", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Status.PENDING, "SUMMARY-PENDING")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Zatwierdzono 0 zmian")
        self.assertNotContains(response, "Odrzucono 0 zmian")
        self.assertNotContains(response, "Sprawdź oznaczone środki")

    def test_list_view_hides_change_summary_for_manager_and_admin(self):
        manager = User.objects.create_user(username="viewer-manager-summary", password="test-pass-123")
        manager.profile.role = UserProfile.Role.MANAGER
        manager.profile.save(update_fields=["role"])
        admin = User.objects.create_user(username="viewer-admin-summary", password="test-pass-123")
        admin.profile.role = UserProfile.Role.ADMIN
        admin.profile.save(update_fields=["role"])
        self._create_change_request(manager, AssetChangeRequest.Status.APPROVED, "SUMMARY-MANAGER")
        self._create_change_request(admin, AssetChangeRequest.Status.REJECTED, "SUMMARY-ADMIN")

        self.client.force_login(manager)
        manager_response = self.client.get(reverse("assets:list"))
        self.client.force_login(admin)
        admin_response = self.client.get(reverse("assets:list"))

        self.assertEqual(manager_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        self.assertNotContains(manager_response, "Zatwierdzono 1 zmian")
        self.assertNotContains(manager_response, "Odrzucono 0 zmian")
        self.assertNotContains(admin_response, "Zatwierdzono 0 zmian")
        self.assertNotContains(admin_response, "Odrzucono 1 zmian")

    def test_list_view_counts_only_current_users_decided_requests(self):
        user = User.objects.create_user(username="viewer-own-summary", password="test-pass-123")
        other_user = User.objects.create_user(username="viewer-other-summary", password="test-pass-123")
        self._create_change_request(user, AssetChangeRequest.Status.APPROVED, "SUMMARY-OWN-APPROVED")
        self._create_change_request(user, AssetChangeRequest.Status.REJECTED, "SUMMARY-OWN-REJECTED")
        self._create_change_request(other_user, AssetChangeRequest.Status.APPROVED, "SUMMARY-OTHER-APPROVED")
        self._create_change_request(other_user, AssetChangeRequest.Status.REJECTED, "SUMMARY-OTHER-REJECTED")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zatwierdzono 1 zmian")
        self.assertContains(response, "Odrzucono 1 zmian")

    def test_list_view_contains_pending_update_marker_renderer(self):
        user = User.objects.create_user(username="viewer-pending-marker", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'pending.textContent = " • Oczekuje";')

    def test_list_view_contains_rejected_update_marker_renderer_with_pending_priority(self):
        user = User.objects.create_user(username="viewer-rejected-marker", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.get(reverse("assets:list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'if (row.has_pending_update) {')
        self.assertContains(response, 'pending.textContent = " • Oczekuje";')
        self.assertContains(response, '} else if (row.has_rejected_update) {')
        self.assertContains(response, 'rejected.textContent = " • Odrzucono";')


class AssetDetailViewTests(TestCase):
    def test_detail_view_redirects_anonymous_user_to_login(self):
        asset = Asset.objects.create(
            name="Detail Anonymous",
            inventory_number="DETAIL-ANON-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_authenticated_user_can_view_asset_detail(self):
        location = Location.objects.create(name="Warehouse")
        asset = Asset.objects.create(
            name="Detail Laptop",
            inventory_number="DETAIL-001",
            status=Asset.Status.ACTIVE,
            location=location.path,
            location_fk=location,
            category="IT",
        )
        user = User.objects.create_user(username="detail-viewer", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Detail Laptop")
        self.assertContains(response, "DETAIL-001")

    def test_detail_view_shows_runtime_inventory_badge(self):
        location = Location.objects.create(name="Detail Runtime Inventory")
        asset = Asset.objects.create(
            name="Detail Runtime Asset",
            inventory_number="DETAIL-RUNTIME-001",
            status=Asset.Status.ACTIVE,
            location=location.path,
            location_fk=location,
        )
        user = User.objects.create_superuser(
            username="detail-runtime-superuser",
            email="detail-runtime-superuser@example.com",
            password="test-pass-123",
        )
        session = InventorySession.objects.create(number="RINV0004", created_by=user)
        InventorySnapshotItem.objects.create(
            session=session,
            asset=asset,
            asset_id_snapshot=asset.pk,
            inventory_number=asset.inventory_number,
            name=asset.name,
            location_fk_id_snapshot=location.pk,
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "STATUS / Aktywny")
        self.assertContains(response, "PROCES / Inwentaryzowany")

    def test_detail_view_renders_current_quantity(self):
        asset = Asset.objects.create(
            name="Detail Quantity",
            inventory_number="DETAIL-QTY-001",
            record_quantity=6,
            current_quantity=4,
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-quantity-fallback",
            email="detail-quantity-fallback@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ILOŚĆ")
        self.assertContains(response, '<div class="detail-value is-mono">4</div>', html=True)

    def test_detail_view_current_quantity_is_independent_from_last_inventory_quantity(self):
        asset = Asset.objects.create(
            name="Detail Quantity Inventory",
            inventory_number="DETAIL-QTY-INVENTORY-001",
            record_quantity=6,
            current_quantity=5,
            last_inventory_quantity=2,
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-quantity-inventory",
            email="detail-quantity-inventory@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ILOŚĆ")
        self.assertContains(response, '<div class="detail-value is-mono">5</div>', html=True)

    def test_scoped_user_can_view_archived_asset_detail(self):
        location = Location.objects.create(name="Archived Detail Warehouse")
        asset = Asset.objects.create(
            name="Archived Detail Laptop",
            inventory_number="DETAIL-ARCHIVE-001",
            status=Asset.Status.LIQUIDATED,
            location=location.path,
            location_fk=location,
            category="IT",
            is_active=False,
        )
        user = User.objects.create_user(username="detail-archive-viewer", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Archived Detail Laptop")
        self.assertContains(response, "DETAIL-ARCHIVE-001")

    def test_detail_view_returns_404_for_asset_outside_user_scope(self):
        root_location = Location.objects.create(name="Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        asset = Asset.objects.create(
            name="Out Of Scope Detail",
            inventory_number="DETAIL-SCOPE-001",
            status=Asset.Status.ACTIVE,
            location=outside_location.path,
            location_fk=outside_location,
            category="IT",
        )
        user = User.objects.create_user(username="detail-scoped-user", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 404)

    def test_detail_view_returns_404_for_user_asset_without_location_fk(self):
        root_location = Location.objects.create(name="Poznan")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        asset = Asset.objects.create(
            name="No FK Detail",
            inventory_number="DETAIL-NO-FK-001",
            status=Asset.Status.ACTIVE,
            location="Legacy only",
            location_fk=None,
            category="IT",
        )
        user = User.objects.create_user(username="detail-no-fk-user", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 404)

    def test_admin_role_and_superuser_can_view_out_of_scope_and_null_location_assets(self):
        outside_location = Location.objects.create(name="Outside")
        out_of_scope_asset = Asset.objects.create(
            name="Admin Out Of Scope Detail",
            inventory_number="DETAIL-ADMIN-SCOPE-001",
            status=Asset.Status.ACTIVE,
            location=outside_location.path,
            location_fk=outside_location,
            category="IT",
        )
        null_location_asset = Asset.objects.create(
            name="Admin Null FK Detail",
            inventory_number="DETAIL-ADMIN-NO-FK-001",
            status=Asset.Status.ACTIVE,
            location="Legacy only",
            location_fk=None,
            category="IT",
        )
        admin_user = User.objects.create_user(username="detail-admin-role", password="test-pass-123")
        admin_user.profile.role = UserProfile.Role.ADMIN
        admin_user.profile.save(update_fields=["role"])
        superuser = User.objects.create_superuser(
            username="detail-superuser",
            email="detail-superuser@example.com",
            password="test-pass-123",
        )

        for user in (admin_user, superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                out_of_scope_response = self.client.get(reverse("assets:detail", kwargs={"id": out_of_scope_asset.id}))
                null_location_response = self.client.get(reverse("assets:detail", kwargs={"id": null_location_asset.id}))
                self.client.logout()

                self.assertEqual(out_of_scope_response.status_code, 200)
                self.assertEqual(null_location_response.status_code, 200)

    def test_detail_view_links_to_public_update_url(self):
        asset = Asset.objects.create(
            name="Public Update Link",
            inventory_number="DETAIL-UPDATE-LINK-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-update-link",
            email="detail-update-link@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("assets:update", kwargs={"pk": asset.id}))

    def test_detail_view_shows_asset_history_section(self):
        asset = Asset.objects.create(
            name="History Detail",
            inventory_number="DETAIL-HISTORY-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-history-superuser",
            email="detail-history-superuser@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Historia środka")

    def test_detail_view_renders_history_entry_values_and_operator(self):
        asset = Asset.objects.create(
            name="History Values Detail",
            inventory_number="DETAIL-HISTORY-VALUES-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        operator = User.objects.create_user(
            username="history-operator",
            first_name="Anna",
            last_name="Kowalska",
            password="test-pass-123",
        )
        viewer = User.objects.create_superuser(
            username="detail-history-values-superuser",
            email="detail-history-values-superuser@example.com",
            password="test-pass-123",
        )
        AssetHistoryEntry.objects.create(
            asset=asset,
            operator=operator,
            event_type=AssetHistoryEntry.EventType.UPDATED,
            description="Zmieniono status",
            old_value="Aktywny",
            new_value="Nieaktywny",
            field_name="status",
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zmieniono status")
        self.assertContains(response, "Anna Kowalska")
        self.assertContains(response, "Nieaktywny")
        self.assertContains(response, "Aktywny")

    def test_detail_view_without_history_shows_empty_state(self):
        asset = Asset.objects.create(
            name="No History Detail",
            inventory_number="DETAIL-HISTORY-EMPTY-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-history-empty-superuser",
            email="detail-history-empty-superuser@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Brak historii środka.")

    def test_detail_history_respects_asset_detail_scope(self):
        allowed_location = Location.objects.create(name="Detail History Allowed")
        outside_location = Location.objects.create(name="Detail History Outside")
        asset = Asset.objects.create(
            name="Out Of Scope History Detail",
            inventory_number="DETAIL-HISTORY-SCOPE-001",
            status=Asset.Status.ACTIVE,
            location=outside_location.path,
            location_fk=outside_location,
            category="IT",
        )
        AssetHistoryEntry.objects.create(
            asset=asset,
            event_type=AssetHistoryEntry.EventType.UPDATED,
            description="Zmieniono nazwę",
            old_value="Old",
            new_value="New",
            field_name="name",
        )
        user = User.objects.create_user(username="detail-history-scoped-user", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 404)

    def test_detail_view_renders_system_for_history_without_operator(self):
        asset = Asset.objects.create(
            name="System History Detail",
            inventory_number="DETAIL-HISTORY-SYSTEM-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        viewer = User.objects.create_superuser(
            username="detail-history-system-superuser",
            email="detail-history-system-superuser@example.com",
            password="test-pass-123",
        )
        AssetHistoryEntry.objects.create(
            asset=asset,
            operator=None,
            event_type=AssetHistoryEntry.EventType.UPDATED,
            description="Zmieniono nazwę",
            old_value="Stara nazwa",
            new_value="Nowa nazwa",
            field_name="name",
        )
        self.client.force_login(viewer)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "System")

    def test_active_asset_does_not_show_restore_button(self):
        asset = Asset.objects.create(
            name="Active Restore Hidden",
            inventory_number="DETAIL-RESTORE-ACTIVE-001",
            status=Asset.Status.ACTIVE,
            location="Warehouse",
            category="IT",
        )
        user = User.objects.create_superuser(
            username="detail-restore-active-superuser",
            email="detail-restore-active-superuser@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Przywróć do Ewidencji")
        self.assertNotContains(response, reverse("assets:asset-restore", kwargs={"id": asset.id}))

    def test_archived_asset_shows_restore_button_for_manager(self):
        location = Location.objects.create(name="Detail Restore Allowed")
        asset = Asset.objects.create(
            name="Archived Restore Visible",
            inventory_number="DETAIL-RESTORE-MANAGER-001",
            status=Asset.Status.LIQUIDATED,
            location=location.path,
            location_fk=location,
            category="IT",
            is_active=False,
        )
        user = User.objects.create_user(username="detail-restore-manager", password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Przywróć do Ewidencji")
        self.assertContains(response, reverse("assets:asset-restore", kwargs={"id": asset.id}))

    def test_archived_asset_does_not_show_restore_button_for_regular_user(self):
        location = Location.objects.create(name="Detail Restore User")
        asset = Asset.objects.create(
            name="Archived Restore Hidden",
            inventory_number="DETAIL-RESTORE-USER-001",
            status=Asset.Status.LIQUIDATED,
            location=location.path,
            location_fk=location,
            category="IT",
            is_active=False,
        )
        user = User.objects.create_user(username="detail-restore-user", password="test-pass-123")
        user.profile.allowed_locations.add(location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Przywróć do Ewidencji")
        self.assertNotContains(response, reverse("assets:asset-restore", kwargs={"id": asset.id}))


class AssetRestoreViewTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Restore Location")
        self.other_location = Location.objects.create(name="Restore Other")

    def _create_asset(self, inventory_number="RESTORE-001", **overrides):
        defaults = {
            "name": "Restore Asset",
            "inventory_number": inventory_number,
            "status": Asset.Status.LIQUIDATED,
            "location": self.location.path,
            "location_fk": self.location,
            "category": "IT",
            "is_active": False,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _admin(self, username="restore-admin"):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])
        return user

    def _manager(self, username="restore-manager", location=None):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location or self.location)
        return user

    def _regular_user(self, username="restore-user", location=None):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.allowed_locations.add(location or self.location)
        return user

    def _restore(self, asset):
        return self.client.post(reverse("assets:asset-restore", kwargs={"id": asset.id}))

    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_admin_can_restore_archived_asset(self):
        asset = self._create_asset("RESTORE-ADMIN-001")
        self.client.force_login(self._admin())

        response = self._restore(asset)

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(self._messages(response), ["Środek został przywrócony do Ewidencji."])

    def test_manager_can_restore_archived_asset_in_scope(self):
        asset = self._create_asset("RESTORE-MANAGER-001")
        self.client.force_login(self._manager())

        response = self._restore(asset)

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)

    def test_manager_cannot_restore_asset_outside_scope(self):
        asset = self._create_asset(
            "RESTORE-SCOPE-001",
            location=self.other_location.path,
            location_fk=self.other_location,
        )
        self.client.force_login(self._manager())

        response = self._restore(asset)

        self.assertEqual(response.status_code, 404)
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_regular_user_cannot_restore_asset(self):
        asset = self._create_asset("RESTORE-USER-001")
        self.client.force_login(self._regular_user())

        response = self._restore(asset)

        self.assertEqual(response.status_code, 403)
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_cannot_restore_active_asset(self):
        asset = self._create_asset("RESTORE-ACTIVE-001", status=Asset.Status.ACTIVE, is_active=True)
        self.client.force_login(self._admin("restore-active-admin"))

        response = self._restore(asset)

        self.assertEqual(response.status_code, 400)
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_restore_creates_history_entry(self):
        asset = self._create_asset("RESTORE-HISTORY-001")
        user = self._manager("restore-history-manager")
        self.client.force_login(user)

        self._restore(asset)

        entry = AssetHistoryEntry.objects.get(asset=asset)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.RESTORED)
        self.assertEqual(entry.description, "Przywrócono środek z Archiwum do Ewidencji.")
        self.assertEqual(entry.field_name, "is_active")
        self.assertEqual(entry.old_value, "Archiwum")
        self.assertEqual(entry.new_value, "Aktywna Ewidencja")
        self.assertEqual(entry.operator, user)

    def test_restore_moves_asset_from_archive_to_active_list(self):
        asset = self._create_asset("RESTORE-LISTS-001")
        self.client.force_login(self._manager("restore-lists-manager"))

        self._restore(asset)

        active_response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})
        archive_response = self.client.get(
            reverse("assets:api-list"),
            {"asset_scope": "archive", "search": asset.inventory_number},
        )

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_response.json()["pagination"]["total_items"], 1)
        self.assertEqual(active_response.json()["results"][0]["inventory_number"], asset.inventory_number)
        self.assertEqual(archive_response.status_code, 200)
        self.assertEqual(archive_response.json()["pagination"]["total_items"], 0)


class AssetWithdrawViewTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Withdraw Location")
        self.other_location = Location.objects.create(name="Withdraw Other")
        self.user = User.objects.create_user(username="withdraw-user", password="test-pass-123")
        self.user.profile.role = UserProfile.Role.MANAGER
        self.user.profile.save(update_fields=["role"])
        self.user.profile.allowed_locations.add(self.location)
        self.client.force_login(self.user)

    def _create_asset(self, inventory_number="WITHDRAW-001", **overrides):
        defaults = {
            "name": "Withdraw Asset",
            "inventory_number": inventory_number,
            "status": Asset.Status.ACTIVE,
            "location": self.location.path,
            "location_fk": self.location,
            "category": "IT",
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def _withdraw(self, asset, status=Asset.Status.LIQUIDATED, **overrides):
        data = {"status": status}
        data.update(overrides)
        return self.client.post(reverse("assets:asset-withdraw", kwargs={"id": asset.id}), data)

    def test_withdraw_sets_inactive_and_status(self):
        asset = self._create_asset("WITHDRAW-STATUS-001")

        response = self._withdraw(asset, Asset.Status.LIQUIDATED)

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)

    def test_withdraw_creates_business_history_entry(self):
        asset = self._create_asset("WITHDRAW-HISTORY-001")

        self._withdraw(asset, Asset.Status.LIQUIDATED)

        entry = AssetHistoryEntry.objects.get(asset=asset)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.WITHDRAWN)
        self.assertEqual(entry.description, "Wycofano środek z aktywnej ewidencji")
        self.assertEqual(entry.field_name, "is_active")
        self.assertEqual(entry.old_value, "Aktywna Ewidencja")
        self.assertEqual(entry.new_value, "Archiwum - Zlikwidowany")
        self.assertEqual(entry.operator, self.user)

    def test_withdraw_moves_asset_from_active_list_to_archive(self):
        asset = self._create_asset("WITHDRAW-LISTS-001")

        self._withdraw(asset, Asset.Status.LIQUIDATED)

        active_response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})
        archive_response = self.client.get(
            reverse("assets:api-list"),
            {"asset_scope": "archive", "search": asset.inventory_number},
        )

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(active_response.json()["pagination"]["total_items"], 0)
        self.assertEqual(archive_response.status_code, 200)
        self.assertEqual(archive_response.json()["pagination"]["total_items"], 1)
        self.assertEqual(archive_response.json()["results"][0]["inventory_number"], asset.inventory_number)

    def test_withdrawn_asset_detail_still_works_and_shows_archive_notice(self):
        asset = self._create_asset("WITHDRAW-DETAIL-001")
        self._withdraw(asset, Asset.Status.LIQUIDATED)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Withdraw Asset")
        self.assertContains(response, "Środek znajduje się w Archiwum")

    def test_cannot_withdraw_already_archived_asset(self):
        asset = self._create_asset("WITHDRAW-ARCHIVED-001", is_active=False, status=Asset.Status.LIQUIDATED)

        response = self._withdraw(asset, Asset.Status.LIQUIDATED)

        self.assertEqual(response.status_code, 400)
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_withdraw_rejects_invalid_status(self):
        asset = self._create_asset("WITHDRAW-INVALID-001")

        response = self._withdraw(asset, Asset.Status.INACTIVE)

        self.assertEqual(response.status_code, 400)
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_withdraw_respects_location_scope(self):
        asset = self._create_asset(
            "WITHDRAW-SCOPE-001",
            location=self.other_location.path,
            location_fk=self.other_location,
        )

        response = self._withdraw(asset, Asset.Status.LIQUIDATED)

        self.assertEqual(response.status_code, 404)
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_regular_asset_rejects_partial_withdraw_quantity(self):
        asset = self._create_asset("WITHDRAW-REGULAR-PARTIAL-001", record_quantity=10, current_quantity=10)

        response = self._withdraw(asset, Asset.Status.LIQUIDATED, withdraw_quantity="3")

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(asset.current_quantity, 10)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())
        self.assertEqual(
            self._messages(response),
            ["Częściowe wycofanie jest dostępne tylko dla aktywnych środków ilościowych."],
        )

    def test_quantity_asset_partial_withdraw_updates_current_quantity(self):
        asset = self._create_asset(
            "WITHDRAW-QTY-PARTIAL-RECORD-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=10,
            current_quantity=10,
        )

        response = self._withdraw(asset, Asset.Status.LIQUIDATED, withdraw_quantity="3")

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(asset.record_quantity, 10)
        self.assertIsNone(asset.last_inventory_quantity)
        self.assertEqual(asset.current_quantity, 7)
        entry = AssetHistoryEntry.objects.get(asset=asset)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.UPDATED)
        self.assertEqual(entry.field_name, "current_quantity")
        self.assertEqual(entry.old_value, "10")
        self.assertEqual(entry.new_value, "7")
        self.assertEqual(entry.operator, self.user)
        self.assertIn("wycofano 3", entry.description)
        self.assertEqual(self._messages(response), ["Część ilości środka została wycofana."])

        active_response = self.client.get(reverse("assets:api-list"), {"search": asset.inventory_number})
        archive_response = self.client.get(
            reverse("assets:api-list"),
            {"asset_scope": "archive", "search": asset.inventory_number},
        )
        self.assertEqual(active_response.json()["pagination"]["total_items"], 1)
        self.assertEqual(archive_response.json()["pagination"]["total_items"], 0)

    def test_quantity_asset_full_withdraw_quantity_archives_asset(self):
        asset = self._create_asset(
            "WITHDRAW-QTY-FULL-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=10,
            current_quantity=10,
        )

        response = self._withdraw(asset, Asset.Status.LIQUIDATED, withdraw_quantity="10")

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertFalse(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.LIQUIDATED)
        self.assertEqual(asset.current_quantity, 10)
        entry = AssetHistoryEntry.objects.get(asset=asset)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.WITHDRAWN)
        self.assertEqual(entry.field_name, "is_active")

    def test_withdraw_quantity_zero_is_rejected_without_changes(self):
        asset = self._create_asset(
            "WITHDRAW-QTY-ZERO-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=10,
            current_quantity=10,
        )

        response = self._withdraw(asset, Asset.Status.LIQUIDATED, withdraw_quantity="0")

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(asset.current_quantity, 10)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_withdraw_quantity_above_current_quantity_is_rejected_without_changes(self):
        asset = self._create_asset(
            "WITHDRAW-QTY-TOO-MANY-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=10,
            current_quantity=10,
        )

        response = self._withdraw(asset, Asset.Status.LIQUIDATED, withdraw_quantity="11")

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(asset.current_quantity, 10)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_partial_withdraw_does_not_update_legacy_inventory_quantity(self):
        asset = self._create_asset(
            "WITHDRAW-QTY-PARTIAL-INVENTORY-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=15,
            current_quantity=10,
            last_inventory_quantity=10,
        )

        response = self._withdraw(asset, Asset.Status.LIQUIDATED, withdraw_quantity="3")

        self.assertRedirects(response, reverse("assets:detail", kwargs={"id": asset.id}))
        asset.refresh_from_db()
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(asset.record_quantity, 15)
        self.assertEqual(asset.last_inventory_quantity, 10)
        self.assertEqual(asset.current_quantity, 7)

    def test_detail_shows_withdraw_quantity_input_for_quantity_asset_with_multiple_quantity(self):
        asset = self._create_asset(
            "WITHDRAW-UI-QTY-001",
            asset_type=Asset.AssetType.QUANTITY,
            record_quantity=5,
            current_quantity=5,
        )

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="withdraw_quantity"')
        self.assertContains(response, 'max="5"')

    def test_detail_hides_withdraw_quantity_input_for_regular_asset(self):
        asset = self._create_asset("WITHDRAW-UI-FIXED-001", record_quantity=5, current_quantity=5)

        response = self.client.get(reverse("assets:detail", kwargs={"id": asset.id}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="withdraw_quantity"')


class AssetUpdateViewTests(TestCase):
    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def _create_location_tree(self):
        root_location = Location.objects.create(name="Warszawa")
        allowed_location = Location.objects.create(name="Biuro", parent=root_location)
        outside_root = Location.objects.create(name="Krakow")
        outside_location = Location.objects.create(name="Magazyn", parent=outside_root)
        return allowed_location, outside_location

    def _create_asset(self, inventory_number="UPDATE-001", location_obj=None, **overrides):
        location_value = location_obj.path if location_obj else "Warehouse"
        defaults = {
            "name": "Update Asset",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "location": location_value,
            "location_fk": location_obj,
            "category": "IT",
            "is_active": True,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _valid_update_payload(self, asset, **overrides):
        payload = {
            "name": "Updated Asset",
            "inventory_number": asset.inventory_number,
            "asset_type": Asset.AssetType.LOW_VALUE,
            "current_quantity": str(asset.current_quantity),
            "category": "Updated IT",
            "manufacturer": "Dell",
            "model": "Latitude",
            "serial_number": "SN-UPDATED",
            "barcode": "",
            "description": "Updated description",
            "purchase_date": "",
            "commissioning_date": "",
            "purchase_value": "",
            "invoice_number": "",
            "external_id": "",
            "cost_center": "",
            "organizational_unit": "",
            "department": "",
            "location_fk": str(asset.location_fk_id) if asset.location_fk_id else "",
            "room": "101",
            "responsible_person": "",
            "current_user": "",
            "status": Asset.Status.INACTIVE,
            "technical_condition": Asset.TechnicalCondition.VERY_GOOD,
            "last_inventory_date": "",
            "next_review_date": "",
            "warranty_until": "",
            "insurance_until": "",
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def _unchanged_update_payload(self, asset):
        return {
            "name": asset.name,
            "inventory_number": asset.inventory_number,
            "asset_type": asset.asset_type,
            "category": asset.category,
            "manufacturer": asset.manufacturer,
            "model": asset.model,
            "serial_number": asset.serial_number,
            "barcode": asset.barcode,
            "description": asset.description,
            "purchase_date": asset.purchase_date.isoformat() if asset.purchase_date else "",
            "commissioning_date": asset.commissioning_date.isoformat() if asset.commissioning_date else "",
            "purchase_value": str(asset.purchase_value) if asset.purchase_value is not None else "",
            "invoice_number": asset.invoice_number,
            "external_id": asset.external_id,
            "cost_center": asset.cost_center,
            "organizational_unit": asset.organizational_unit,
            "department": asset.department,
            "location_fk": str(asset.location_fk_id) if asset.location_fk_id else "",
            "room": asset.room,
            "responsible_person": str(asset.responsible_person_id) if asset.responsible_person_id else "",
            "current_user": str(asset.current_user_id) if asset.current_user_id else "",
            "current_quantity": str(asset.current_quantity),
            "status": asset.status,
            "technical_condition": asset.technical_condition,
            "last_inventory_date": asset.last_inventory_date.isoformat() if asset.last_inventory_date else "",
            "next_review_date": asset.next_review_date.isoformat() if asset.next_review_date else "",
            "warranty_until": asset.warranty_until.isoformat() if asset.warranty_until else "",
            "insurance_until": asset.insurance_until.isoformat() if asset.insurance_until else "",
            "is_active": "on" if asset.is_active else "",
        }

    def _manager_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.role = UserProfile.Role.MANAGER
        user.profile.save(update_fields=["role"])
        user.profile.allowed_locations.add(location)
        return user

    def _user_with_location(self, username, location):
        user = User.objects.create_user(username=username, password="test-pass-123")
        user.profile.allowed_locations.add(location)
        return user

    def test_update_view_redirects_anonymous_user_to_login(self):
        location = Location.objects.create(name="Warehouse")
        asset = self._create_asset(location_obj=location)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_user_in_scope_can_open_update_form(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-in-scope", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, asset.name)

    def test_update_form_uses_edit_title(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-edit-title", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edytuj składnik majątku")
        self.assertNotContains(response, "<h2>Nowy składnik majątku</h2>", html=True)

    def test_update_form_renders_asset_detail_sections(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-sections", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dane podstawowe")
        self.assertContains(response, "Organizacyjne")
        self.assertContains(response, "Techniczne")
        self.assertContains(response, "Finansowe")
        self.assertContains(response, "Eksploatacja")

    def test_update_form_renders_current_quantity_input(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(
            inventory_number="UPDATE-QUANTITY-DISPLAY-001",
            location_obj=allowed_location,
            current_quantity=9,
        )
        user = self._manager_with_location("update-quantity-display", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ilość")
        self.assertContains(response, 'type="number" name="current_quantity"')
        self.assertContains(response, 'value="9"')

    def test_update_form_keeps_legacy_and_active_fields_hidden(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-hidden-system-fields", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="hidden" name="is_active"')
        self.assertContains(response, 'type="number" name="current_quantity"')
        self.assertNotContains(response, 'name="record_quantity"')
        self.assertNotContains(response, 'type="checkbox" name="is_active"')

    def test_update_form_renders_last_inventory_date_as_read_only_display(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(
            inventory_number="UPDATE-LAST-INVENTORY-DATE-001",
            location_obj=allowed_location,
            last_inventory_date=date(2026, 5, 1),
        )
        user = self._manager_with_location("update-last-inventory-display", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data ostatniej inwentaryzacji")
        self.assertContains(response, '<div class="detail-value is-mono">2026-05-01</div>', html=True)
        self.assertContains(response, 'type="hidden" name="last_inventory_date"')
        self.assertNotContains(response, 'type="date" name="last_inventory_date"')

    def test_user_outside_scope_gets_404_for_update_form(self):
        allowed_location, outside_location = self._create_location_tree()
        asset = self._create_asset(location_obj=outside_location)
        user = self._manager_with_location("update-outside-scope", allowed_location)
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 404)

    def test_user_requiring_approval_cannot_queue_update_outside_scope(self):
        allowed_location, outside_location = self._create_location_tree()
        asset = self._create_asset(location_obj=outside_location)
        user = self._manager_with_location("update-approval-outside-scope", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Queue"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_user_cannot_update_asset_without_location_fk(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=None, location_fk=None, location="Legacy only")
        user = self._manager_with_location("update-no-fk", allowed_location)
        self.client.force_login(user)

        get_response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))
        post_response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Save"),
        )

        asset.refresh_from_db()
        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(post_response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")

    def test_user_requiring_approval_cannot_queue_update_for_asset_without_location_fk(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=None, location_fk=None, location="Legacy only")
        user = self._manager_with_location("update-approval-no-fk", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Queue"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_can_open_out_of_scope_update_form(self):
        _, outside_location = self._create_location_tree()
        asset = self._create_asset(location_obj=outside_location)
        user = User.objects.create_user(username="update-admin-role", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.save(update_fields=["role"])
        self.client.force_login(user)

        response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))

        self.assertEqual(response.status_code, 200)

    def test_superuser_can_open_out_of_scope_and_null_location_update_forms(self):
        _, outside_location = self._create_location_tree()
        out_of_scope_asset = self._create_asset(inventory_number="UPDATE-SUPER-001", location_obj=outside_location)
        null_location_asset = self._create_asset(
            inventory_number="UPDATE-SUPER-002",
            location_obj=None,
            location_fk=None,
            location="Legacy only",
        )
        user = User.objects.create_superuser(
            username="update-superuser",
            email="update-superuser@example.com",
            password="test-pass-123",
        )
        self.client.force_login(user)

        out_of_scope_response = self.client.get(reverse("assets:update", kwargs={"pk": out_of_scope_asset.pk}))
        null_location_response = self.client.get(reverse("assets:update", kwargs={"pk": null_location_asset.pk}))

        self.assertEqual(out_of_scope_response.status_code, 200)
        self.assertEqual(null_location_response.status_code, 200)

    def test_valid_update_post_saves_changes_and_redirects_to_detail(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-post", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Saved Update"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("assets:detail", kwargs={"id": asset.pk}))
        self.assertEqual(asset.name, "Saved Update")
        self.assertEqual(asset.location, allowed_location.path)
        self.assertEqual(asset.location_fk, allowed_location)
        self.assertEqual(asset.status, Asset.Status.INACTIVE)
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_archived_asset_edit_is_blocked(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(
            inventory_number="UPDATE-ARCHIVED-001",
            location_obj=allowed_location,
            is_active=False,
            status=Asset.Status.LIQUIDATED,
        )
        user = self._manager_with_location("update-archived", allowed_location)
        self.client.force_login(user)

        get_response = self.client.get(reverse("assets:update", kwargs={"pk": asset.pk}))
        post_response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Should Not Save"),
        )

        asset.refresh_from_db()
        self.assertEqual(get_response.status_code, 404)
        self.assertEqual(post_response.status_code, 404)
        self.assertEqual(asset.name, "Update Asset")
        self.assertFalse(asset.is_active)

    def test_update_without_approval_creates_history_for_changed_field(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Original Name")
        user = self._manager_with_location("update-history", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="History Name"),
        )

        self.assertEqual(response.status_code, 302)
        entry = AssetHistoryEntry.objects.get(asset=asset, field_name="name")
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.UPDATED)
        self.assertEqual(entry.description, "Zmieniono nazwę")
        self.assertEqual(entry.old_value, "Original Name")
        self.assertEqual(entry.new_value, "History Name")
        self.assertEqual(entry.operator, user)
        self.assertEqual(entry.source_object_type, "")
        self.assertIsNone(entry.source_object_id)

    def test_update_history_only_changed_business_fields(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(
            location_obj=allowed_location,
            name="Only Changed Original",
            record_quantity=3,
        )
        user = self._manager_with_location("update-history-only-changed", allowed_location)
        self.client.force_login(user)

        payload = self._unchanged_update_payload(asset)
        payload["description"] = "Technical description change only"
        payload["record_quantity"] = "99"
        response = self.client.post(reverse("assets:update", kwargs={"pk": asset.pk}), data=payload)

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.description, "Technical description change only")
        self.assertEqual(asset.record_quantity, 3)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_update_history_formats_location_status_bool_and_decimal(self):
        allowed_location, _ = self._create_location_tree()
        child_location = Location.objects.create(name="Sala historii", parent=allowed_location)
        asset = self._create_asset(
            location_obj=allowed_location,
            status=Asset.Status.ACTIVE,
            is_active=True,
            purchase_value=Decimal("10.50"),
        )
        user = self._manager_with_location("update-history-format", allowed_location)
        self.client.force_login(user)

        payload = self._unchanged_update_payload(asset)
        payload["location_fk"] = str(child_location.id)
        payload["status"] = Asset.Status.INACTIVE
        payload["purchase_value"] = "20.75"
        payload.pop("is_active")
        response = self.client.post(reverse("assets:update", kwargs={"pk": asset.pk}), data=payload)

        self.assertEqual(response.status_code, 302)
        entries = {
            entry.field_name: entry
            for entry in AssetHistoryEntry.objects.filter(asset=asset)
        }
        self.assertEqual(entries["location_fk"].old_value, allowed_location.path)
        self.assertEqual(entries["location_fk"].new_value, child_location.path)
        self.assertEqual(entries["status"].old_value, "Aktywny")
        self.assertEqual(entries["status"].new_value, "Nieaktywny")
        self.assertNotIn("is_active", entries)
        self.assertEqual(entries["purchase_value"].old_value, "10.50")
        self.assertEqual(entries["purchase_value"].new_value, "20.75")

    def test_update_without_changes_creates_no_history_entries(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-history-no-change", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._unchanged_update_payload(asset),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AssetHistoryEntry.objects.filter(asset=asset).exists())

    def test_user_requiring_approval_queues_update_without_saving_asset(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Original Name", location=allowed_location.path)
        user = self._user_with_location("update-approval-required", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(
                asset,
                name="Queued Update",
                malicious_field="not-in-payload",
            ),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("assets:detail", kwargs={"id": asset.pk}))
        self.assertEqual(asset.name, "Original Name")
        self.assertEqual(asset.location, allowed_location.path)
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        change_request = AssetChangeRequest.objects.get()
        self.assertEqual(change_request.operation, AssetChangeRequest.Operation.UPDATE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(change_request.asset, asset)
        self.assertEqual(change_request.requested_by, user)
        self.assertSetEqual(set(change_request.payload.keys()), {"current", "proposed"})
        self.assertEqual(change_request.payload["current"]["name"], "Original Name")
        self.assertEqual(change_request.payload["current"]["location_fk"], allowed_location.id)
        self.assertEqual(change_request.payload["proposed"]["name"], "Queued Update")
        self.assertEqual(change_request.payload["proposed"]["location_fk"], allowed_location.id)
        self.assertNotIn("record_quantity", change_request.payload["current"])
        self.assertNotIn("record_quantity", change_request.payload["proposed"])
        self.assertNotIn("is_active", change_request.payload["current"])
        self.assertNotIn("is_active", change_request.payload["proposed"])
        self.assertNotIn("last_inventory_date", change_request.payload["current"])
        self.assertNotIn("last_inventory_date", change_request.payload["proposed"])
        self.assertNotIn("malicious_field", change_request.payload["current"])
        self.assertNotIn("malicious_field", change_request.payload["proposed"])
        self.assertEqual(self._messages(response), ["Zmiana została przekazana do akceptacji."])

    def test_user_requiring_approval_updates_existing_pending_update_request(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Original Name", location=allowed_location.path)
        user = self._user_with_location("update-pending-same-user", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        first_response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="First Queued Update", room="101"),
        )
        self.assertEqual(self._messages(first_response), ["Zmiana została przekazana do akceptacji."])
        first_request = AssetChangeRequest.objects.get()

        second_response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Second Queued Update", room="202"),
        )

        asset.refresh_from_db()
        first_request.refresh_from_db()
        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        self.assertEqual(first_request.operation, AssetChangeRequest.Operation.UPDATE)
        self.assertEqual(first_request.status, AssetChangeRequest.Status.PENDING)
        self.assertEqual(first_request.asset, asset)
        self.assertEqual(first_request.requested_by, user)
        self.assertEqual(first_request.payload["current"]["name"], "Original Name")
        self.assertEqual(first_request.payload["proposed"]["name"], "Second Queued Update")
        self.assertEqual(first_request.payload["proposed"]["room"], "202")
        self.assertEqual(asset.name, "Original Name")
        self.assertEqual(asset.room, "")
        self.assertIn("Oczekująca zmiana została zaktualizowana.", self._messages(second_response))

    def test_user_requiring_approval_cannot_overwrite_another_users_pending_request(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Original Name", location=allowed_location.path)
        first_user = self._user_with_location("update-cross-user-first", allowed_location)
        first_user.profile.asset_changes_require_approval = True
        first_user.profile.save(update_fields=["asset_changes_require_approval"])
        second_user = self._user_with_location("update-cross-user-second", allowed_location)
        second_user.profile.asset_changes_require_approval = True
        second_user.profile.save(update_fields=["asset_changes_require_approval"])

        self.client.force_login(first_user)
        self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="First User Change"),
        )
        first_request = AssetChangeRequest.objects.get()

        second_client = Client()
        second_client.force_login(second_user)
        second_response = second_client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Second User Overwrite Attempt"),
        )

        first_request.refresh_from_db()
        asset.refresh_from_db()
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        self.assertEqual(first_request.requested_by, first_user)
        self.assertEqual(first_request.payload["proposed"]["name"], "First User Change")
        self.assertEqual(asset.name, "Original Name")
        self.assertIn(
            "Dla tego środka istnieje już oczekujący wniosek innego użytkownika.",
            self._messages(second_response),
        )

    def test_user_requiring_approval_creates_separate_pending_update_for_other_asset(self):
        allowed_location, _ = self._create_location_tree()
        first_asset = self._create_asset(
            inventory_number="UPDATE-PENDING-OTHER-001",
            location_obj=allowed_location,
            name="First Original",
        )
        second_asset = self._create_asset(
            inventory_number="UPDATE-PENDING-OTHER-002",
            location_obj=allowed_location,
            name="Second Original",
        )
        user = self._user_with_location("update-pending-other-asset", allowed_location)
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        self.client.post(
            reverse("assets:update", kwargs={"pk": first_asset.pk}),
            data=self._valid_update_payload(first_asset, name="First Queued"),
        )
        response = self.client.post(
            reverse("assets:update", kwargs={"pk": second_asset.pk}),
            data=self._valid_update_payload(second_asset, name="Second Queued"),
        )

        first_asset.refresh_from_db()
        second_asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AssetChangeRequest.objects.count(), 2)
        self.assertTrue(
            AssetChangeRequest.objects.filter(
                asset=first_asset,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
            ).exists()
        )
        self.assertTrue(
            AssetChangeRequest.objects.filter(
                asset=second_asset,
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
            ).exists()
        )
        self.assertEqual(first_asset.name, "First Original")
        self.assertEqual(second_asset.name, "Second Original")

    def test_invalid_update_post_does_not_save_changes(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-invalid", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="", inventory_number=""),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(asset.name, "Update Asset")
        self.assertEqual(asset.inventory_number, "UPDATE-001")
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertIn("name", response.context["form"].errors)
        self.assertIn("inventory_number", response.context["form"].errors)

    def test_update_ignores_extra_post_fields(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-extra-field", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, malicious_field="ignored"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(hasattr(asset, "malicious_field"))

    def test_update_ignores_system_managed_post_fields(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(
            inventory_number="UPDATE-SYSTEM-FIELDS-001",
            location_obj=allowed_location,
            record_quantity=5,
            is_active=True,
            last_inventory_date=date(2026, 4, 1),
        )
        user = self._manager_with_location("update-system-fields", allowed_location)
        self.client.force_login(user)
        payload = self._valid_update_payload(
            asset,
            name="Saved Business Update",
            record_quantity="99",
            last_inventory_date="2026-05-01",
        )
        payload.pop("is_active")

        response = self.client.post(reverse("assets:update", kwargs={"pk": asset.pk}), data=payload)

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.name, "Saved Business Update")
        self.assertEqual(asset.record_quantity, 5)
        self.assertTrue(asset.is_active)
        self.assertEqual(asset.last_inventory_date, date(2026, 4, 1))

    def test_update_changes_location_through_location_fk_and_syncs_cache(self):
        allowed_location, _ = self._create_location_tree()
        child_location = Location.objects.create(name="Sala 2", parent=allowed_location)
        asset = self._create_asset(location_obj=allowed_location)
        user = self._manager_with_location("update-location-fk", allowed_location)
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, location_fk=str(child_location.id)),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.location, child_location.path)
        self.assertEqual(asset.location_fk, child_location)

        detail_response = self.client.get(reverse("assets:detail", kwargs={"id": asset.pk}))
        self.assertEqual(detail_response.status_code, 200)

    def test_superuser_updates_asset_without_queue(self):
        allowed_location, _ = self._create_location_tree()
        superuser = User.objects.create_superuser(
            username="update-bypass-superuser",
            email="update-bypass-superuser@example.com",
            password="test-pass-123",
        )
        superuser.profile.asset_changes_require_approval = True
        superuser.profile.save(update_fields=["asset_changes_require_approval"])
        asset = self._create_asset(
            inventory_number="UPDATE-BYPASS-001",
            location_obj=allowed_location,
            name="Bypass Original",
        )
        self.client.force_login(superuser)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Bypass Saved"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.name, "Bypass Saved")
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_with_approval_required_updates_asset_directly(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Admin Original")
        user = User.objects.create_user(username="update-admin-approval-required", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Admin Direct Despite Flag"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.name, "Admin Direct Despite Flag")
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_without_approval_required_updates_asset_directly(self):
        allowed_location, _ = self._create_location_tree()
        asset = self._create_asset(location_obj=allowed_location, name="Admin Direct Original")
        user = User.objects.create_user(username="update-admin-direct", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = False
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:update", kwargs={"pk": asset.pk}),
            data=self._valid_update_payload(asset, name="Admin Direct Saved"),
        )

        asset.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(asset.name, "Admin Direct Saved")
        self.assertFalse(AssetChangeRequest.objects.exists())


class AssetCreateViewTests(TestCase):
    def _valid_asset_payload(self, **overrides):
        location, _ = Location.objects.get_or_create(name="Create Warehouse")
        payload = {
            "name": "Created Asset",
            "inventory_number": "CREATE-001",
            "asset_type": Asset.AssetType.FIXED,
            "current_quantity": "1",
            "status": Asset.Status.ACTIVE,
            "technical_condition": Asset.TechnicalCondition.GOOD,
            "location_fk": str(location.id),
            "is_active": "on",
        }
        payload.update(overrides)
        return payload

    def test_create_view_redirects_anonymous_user_and_does_not_create_asset(self):
        response = self.client.post(reverse("assets:create"), data=self._valid_asset_payload())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-001").exists())

    def test_authenticated_user_can_create_asset(self):
        user = User.objects.create_user(username="asset-creator", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.post(reverse("assets:create"), data=self._valid_asset_payload(current_quantity="6"))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertEqual(Asset.objects.filter(inventory_number="CREATE-001").count(), 1)
        asset = Asset.objects.get(inventory_number="CREATE-001")
        self.assertEqual(asset.name, "Created Asset")
        self.assertEqual(asset.asset_type, Asset.AssetType.FIXED)
        self.assertEqual(asset.current_quantity, 6)
        self.assertEqual(asset.status, Asset.Status.ACTIVE)
        self.assertEqual(asset.technical_condition, Asset.TechnicalCondition.GOOD)
        self.assertIsNotNone(asset.location_fk)
        self.assertEqual(asset.location, asset.location_fk.path)

    def test_create_ignores_system_managed_post_fields(self):
        user = User.objects.create_user(username="asset-create-system-fields", password="test-pass-123")
        self.client.force_login(user)

        payload = self._valid_asset_payload(
            inventory_number="CREATE-SYSTEM-FIELDS-001",
            current_quantity="8",
            record_quantity="99",
            last_inventory_date="2026-05-01",
        )
        payload.pop("is_active")
        response = self.client.post(reverse("assets:create"), data=payload)

        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get(inventory_number="CREATE-SYSTEM-FIELDS-001")
        self.assertEqual(asset.current_quantity, 8)
        self.assertEqual(asset.record_quantity, 1)
        self.assertTrue(asset.is_active)
        self.assertIsNone(asset.last_inventory_date)

    def test_create_without_approval_creates_history_entry(self):
        user = User.objects.create_user(username="asset-history-creator", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(inventory_number="CREATE-HISTORY-001"),
        )

        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get(inventory_number="CREATE-HISTORY-001")
        entry = AssetHistoryEntry.objects.get(asset=asset)
        self.assertEqual(entry.event_type, AssetHistoryEntry.EventType.CREATED)
        self.assertEqual(entry.description, "Utworzono środek")
        self.assertEqual(entry.old_value, "")
        self.assertEqual(entry.new_value, "")
        self.assertEqual(entry.field_name, "")
        self.assertEqual(entry.operator, user)
        self.assertEqual(entry.source_object_type, "")
        self.assertIsNone(entry.source_object_id)

    def test_invalid_create_form_does_not_create_asset(self):
        user = User.objects.create_user(username="asset-invalid", password="test-pass-123")
        self.client.force_login(user)
        asset_count = Asset.objects.count()

        response = self.client.post(reverse("assets:create"), data={})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Asset.objects.count(), asset_count)
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertIn("name", response.context["form"].errors)
        self.assertIn("inventory_number", response.context["form"].errors)

    def test_user_requiring_approval_creates_change_request_without_asset(self):
        user = User.objects.create_user(username="asset-approval-required", password="test-pass-123")
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-APPROVAL-001",
                name="Queued Asset",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-APPROVAL-001").exists())
        self.assertEqual(AssetChangeRequest.objects.count(), 1)
        change_request = AssetChangeRequest.objects.get()
        self.assertEqual(change_request.operation, AssetChangeRequest.Operation.CREATE)
        self.assertEqual(change_request.status, AssetChangeRequest.Status.PENDING)
        self.assertIsNone(change_request.asset)
        self.assertEqual(change_request.requested_by, user)
        self.assertEqual(change_request.payload["name"], "Queued Asset")
        self.assertEqual(change_request.payload["inventory_number"], "CREATE-APPROVAL-001")
        self.assertEqual(change_request.payload["asset_type"], Asset.AssetType.FIXED)
        self.assertEqual(change_request.payload["current_quantity"], 1)
        self.assertEqual(change_request.payload["status"], Asset.Status.ACTIVE)
        self.assertEqual(change_request.payload["technical_condition"], Asset.TechnicalCondition.GOOD)
        self.assertIn("location_fk", change_request.payload)
        self.assertNotIn("record_quantity", change_request.payload)
        self.assertNotIn("is_active", change_request.payload)
        self.assertNotIn("last_inventory_date", change_request.payload)

    def test_approval_payload_uses_cleaned_data_and_ignores_extra_post_fields(self):
        user = User.objects.create_user(username="asset-approval-payload", password="test-pass-123")
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-PAYLOAD-001",
                malicious_field="should-not-be-saved",
            ),
        )

        self.assertEqual(response.status_code, 302)
        change_request = AssetChangeRequest.objects.get()
        self.assertNotIn("malicious_field", change_request.payload)

    def test_superuser_creates_asset_without_queue(self):
        superuser = User.objects.create_superuser(
            username="asset-create-superuser",
            email="asset-create-superuser@example.com",
            password="test-pass-123",
        )
        superuser.profile.asset_changes_require_approval = True
        superuser.profile.save(update_fields=["asset_changes_require_approval"])
        self.client.force_login(superuser)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(inventory_number="CREATE-BYPASS-001"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(inventory_number="CREATE-BYPASS-001").exists())
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_user_with_approver_flag_and_approval_required_creates_queue_not_asset(self):
        user = User.objects.create_user(username="asset-create-user-flag", password="test-pass-123")
        user.profile.can_approve_asset_changes = True
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["can_approve_asset_changes", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(inventory_number="CREATE-USER-FLAG-001"),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-USER-FLAG-001").exists())
        self.assertEqual(AssetChangeRequest.objects.filter(
            operation=AssetChangeRequest.Operation.CREATE,
            status=AssetChangeRequest.Status.PENDING,
        ).count(), 1)

    def test_admin_role_with_approval_required_creates_asset_directly(self):
        user = User.objects.create_user(username="asset-create-admin-approval-required", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = True
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-ADMIN-APPROVAL-001",
                name="Admin Direct Despite Flag",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(inventory_number="CREATE-ADMIN-APPROVAL-001").exists())
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_admin_role_without_approval_required_creates_asset_directly(self):
        user = User.objects.create_user(username="asset-create-admin-direct", password="test-pass-123")
        user.profile.role = UserProfile.Role.ADMIN
        user.profile.asset_changes_require_approval = False
        user.profile.save(update_fields=["role", "asset_changes_require_approval"])
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-ADMIN-DIRECT-001",
                name="Admin Direct Asset",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(inventory_number="CREATE-ADMIN-DIRECT-001").exists())
        self.assertFalse(AssetChangeRequest.objects.exists())

    def test_create_uses_posted_location_fk_and_syncs_location_cache(self):
        user = User.objects.create_user(username="asset-location-fk", password="test-pass-123")
        location = Location.objects.create(name="Used Location")
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-FK-001",
                location_fk=str(location.id),
            ),
        )

        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get(inventory_number="CREATE-FK-001")
        self.assertEqual(asset.location_fk, location)
        self.assertEqual(asset.location, location.path)

    def test_create_without_location_fk_does_not_create_asset(self):
        user = User.objects.create_user(username="asset-legacy-location", password="test-pass-123")
        self.client.force_login(user)

        response = self.client.post(
            reverse("assets:create"),
            data=self._valid_asset_payload(
                inventory_number="CREATE-LOC-001",
                location_fk="",
                location="LOKALIZACJA SPOZA ZAKRESU",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Asset.objects.filter(inventory_number="CREATE-LOC-001").exists())
        self.assertIn("location_fk", response.context["form"].errors)


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
            status=Asset.Status.ACTIVE,
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
            status=Asset.Status.ACTIVE,
            location=target.path,
            category="IT",
        )
        Asset.objects.create(
            name="Laptop Empty",
            inventory_number="BF-002",
            status=Asset.Status.ACTIVE,
            location="",
            category="IT",
        )
        Asset.objects.create(
            name="Laptop Miss",
            inventory_number="BF-003",
            status=Asset.Status.ACTIVE,
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
            status=Asset.Status.ACTIVE,
            location=target.path,
            category="IT",
        )
        unmatched_asset = Asset.objects.create(
            name="Laptop Miss 2",
            inventory_number="BF-011",
            status=Asset.Status.ACTIVE,
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


class AssetLabelsPdfViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="labels-test-user", password="test-pass-123")
        self.location = Location.objects.create(name="Magazyn etykiet")
        self.asset = Asset.objects.create(
            name="Drukarka etykiet",
            inventory_number="LBL-001",
            barcode="ST260000001",
            status=Asset.Status.ACTIVE,
            location=self.location.path,
            location_fk=self.location,
        )
        self.url = reverse("assets:labels-pdf")

    def test_view_requires_login(self):
        response = self.client.get(self.url, {"ids": str(self.asset.id)})
        self.assertEqual(response.status_code, 302)

    def test_view_returns_pdf(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"ids": str(self.asset.id)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_empty_ids_returns_400(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"ids": ""})
        self.assertEqual(response.status_code, 400)

    def test_invalid_ids_returns_400(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"ids": "abc,def"})
        self.assertEqual(response.status_code, 400)

    def test_no_ids_param_returns_400(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)


class GenerateLabelsPdfTests(TestCase):
    def _make_asset(self, name, barcode, inventory_number):
        location = Location.objects.create(name=f"Loc-{barcode}")
        return Asset.objects.create(
            name=name,
            inventory_number=inventory_number,
            barcode=barcode,
            status=Asset.Status.ACTIVE,
            location=location.path,
            location_fk=location,
        )

    def test_pdf_not_empty(self):
        from .labels import generate_labels_pdf
        asset = self._make_asset("Biurko", "ST260000010", "INW-010")
        buffer = generate_labels_pdf([asset], "GMINA")
        self.assertGreater(len(buffer.getvalue()), 0)

    def test_correct_number_of_pages(self):
        from .labels import generate_labels_pdf
        asset1 = self._make_asset("Biurko", "ST260000020", "INW-020")
        asset2 = self._make_asset("Krzesło", "ST260000021", "INW-021")
        buffer = generate_labels_pdf([asset1, asset2], "GMINA")
        raw = buffer.getvalue()
        # /Type /Pages = parent node (1); /Type /Page = individual pages (N)
        page_count = raw.count(b"/Type /Page") - raw.count(b"/Type /Pages")
        self.assertEqual(page_count, 2)


class AssetLtDocumentViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.location_a = Location.objects.create(name="LT Warszawa")
        cls.location_b = Location.objects.create(name="LT Krakow")

        cls.archived_asset = Asset.objects.create(
            name="Biurko archiwalne",
            inventory_number="LT-INW-001",
            barcode="LT-BC-001",
            status=Asset.Status.LIQUIDATED,
            location=cls.location_a.path,
            location_fk=cls.location_a,
            is_active=False,
        )
        cls.active_asset = Asset.objects.create(
            name="Monitor aktywny",
            inventory_number="LT-INW-002",
            barcode="LT-BC-002",
            status=Asset.Status.ACTIVE,
            location=cls.location_a.path,
            location_fk=cls.location_a,
            is_active=True,
        )
        cls.archived_other_location = Asset.objects.create(
            name="Szafa archiwalna",
            inventory_number="LT-INW-003",
            barcode="LT-BC-003",
            status=Asset.Status.LIQUIDATED,
            location=cls.location_b.path,
            location_fk=cls.location_b,
            is_active=False,
        )

        cls.admin_user = User.objects.create_superuser(
            username="lt-admin", password="pass123"
        )
        cls.scoped_user = User.objects.create_user(
            username="lt-scoped", password="pass123"
        )
        cls.scoped_user.profile.role = UserProfile.Role.MANAGER
        cls.scoped_user.profile.save()
        cls.scoped_user.profile.allowed_locations.add(cls.location_a)

    def _url(self, *ids):
        return reverse("assets:archive-lt") + "?ids=" + ",".join(str(i) for i in ids)

    def test_anonymous_redirects_to_login(self):
        response = self.client.get(self._url(self.archived_asset.pk))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_active_asset_excluded_from_lt(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(self.active_asset.pk))
        self.assertEqual(response.status_code, 404)

    def test_archived_asset_appears_in_lt(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(self.archived_asset.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LT-INW-001")
        self.assertContains(response, "Biurko archiwalne")

    def test_active_and_archived_mixed_only_archived_shown(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(
            self._url(self.archived_asset.pk, self.active_asset.pk)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LT-INW-001")
        self.assertNotContains(response, "LT-INW-002")

    def test_scoped_user_sees_asset_in_allowed_location(self):
        self.client.force_login(self.scoped_user)
        response = self.client.get(self._url(self.archived_asset.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "LT-INW-001")

    def test_scoped_user_cannot_see_asset_outside_scope(self):
        self.client.force_login(self.scoped_user)
        response = self.client.get(self._url(self.archived_other_location.pk))
        self.assertEqual(response.status_code, 404)

    def test_template_contains_lt_title(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(self.archived_asset.pk))
        self.assertContains(response, "LT — Likwidacja")

    def test_template_contains_signature_sections(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(self.archived_asset.pk))
        self.assertContains(response, "Sporządził")
        self.assertContains(response, "Zatwierdził")

    def test_template_contains_remarks_section(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(self._url(self.archived_asset.pk))
        self.assertContains(response, "Uwagi")

    def test_no_ids_returns_400(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("assets:archive-lt"))
        self.assertEqual(response.status_code, 400)

    def test_invalid_ids_returns_400(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("assets:archive-lt") + "?ids=abc,xyz")
        self.assertEqual(response.status_code, 400)

    def test_rejects_post_request(self):
        self.client.force_login(self.admin_user)
        response = self.client.post(self._url(self.archived_asset.pk))
        self.assertEqual(response.status_code, 405)


# ─── Importer tests ───────────────────────────────────────────────────────────

import io as _io
import openpyxl as _openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from .importer import (
    parse_import_xlsx,
    resolve_location_path,
    import_assets_from_rows,
    _validate_row,
)


def _make_xlsx_upload(rows, filename="test.xlsx"):
    wb = _openpyxl.Workbook()
    ws = wb.active
    ws.append(["Numer inwentarzowy", "Nazwa", "Ilość", "Wartość", "Lokalizacja"])
    for row in rows:
        ws.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return SimpleUploadedFile(
        filename,
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _make_xlsx_bytes(rows):
    wb = _openpyxl.Workbook()
    ws = wb.active
    ws.append(["Numer inwentarzowy", "Nazwa", "Ilość", "Wartość", "Lokalizacja"])
    for row in rows:
        ws.append(row)
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


class ImporterParseTests(TestCase):
    def test_parse_returns_data_rows(self):
        buf = _make_xlsx_bytes([["INW-001", "Laptop", "1", "1000.00", "Sala A"]])
        rows = parse_import_xlsx(buf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Laptop")
        self.assertEqual(rows[0]["inventory_number"], "INW-001")
        self.assertEqual(rows[0]["quantity"], "1")
        self.assertEqual(rows[0]["value"], "1000.00")
        self.assertEqual(rows[0]["location_path"], "Sala A")

    def test_parse_skips_empty_rows(self):
        buf = _make_xlsx_bytes([
            ["INW-001", "Laptop", "1", "", "Sala A"],
            [None, None, None, None, None],
            ["INW-002", "Krzesło", "2", "", "Sala B"],
        ])
        rows = parse_import_xlsx(buf)
        self.assertEqual(len(rows), 2)

    def test_parse_skips_comment_rows(self):
        buf = _make_xlsx_bytes([
            ["# To jest komentarz", "", "", "", ""],
            ["INW-001", "Laptop", "1", "", "Sala A"],
        ])
        rows = parse_import_xlsx(buf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Laptop")

    def test_parse_row_number_is_correct(self):
        buf = _make_xlsx_bytes([["INW-001", "Laptop", "1", "", "Sala A"]])
        rows = parse_import_xlsx(buf)
        self.assertEqual(rows[0]["row_number"], 2)

    def test_parse_optional_fields_default_to_empty(self):
        buf = _make_xlsx_bytes([["", "Laptop", "", "", "Sala A"]])
        rows = parse_import_xlsx(buf)
        self.assertEqual(rows[0]["inventory_number"], "")
        self.assertEqual(rows[0]["quantity"], "")
        self.assertEqual(rows[0]["value"], "")


class ImporterValidateRowTests(TestCase):
    def _row(self, **kwargs):
        base = {
            "row_number": 2,
            "inventory_number": "",
            "name": "Laptop",
            "quantity": "1",
            "value": "",
            "location_path": "Sala A",
        }
        base.update(kwargs)
        return base

    def test_valid_row_has_no_errors(self):
        self.assertEqual(_validate_row(self._row()), [])

    def test_missing_name_is_error(self):
        errors = _validate_row(self._row(name=""))
        self.assertIn("Brak nazwy", errors)

    def test_missing_location_is_error(self):
        errors = _validate_row(self._row(location_path=""))
        self.assertIn("Brak lokalizacji", errors)

    def test_double_slash_location_is_error(self):
        errors = _validate_row(self._row(location_path="Sala A//Pokój 1"))
        self.assertTrue(any("Niepoprawna lokalizacja" in e for e in errors))

    def test_invalid_quantity_text_is_error(self):
        errors = _validate_row(self._row(quantity="abc"))
        self.assertTrue(any("ilość" in e.lower() for e in errors))

    def test_invalid_quantity_float_is_error(self):
        errors = _validate_row(self._row(quantity="1.5"))
        self.assertTrue(any("ilość" in e.lower() for e in errors))

    def test_zero_quantity_is_error(self):
        errors = _validate_row(self._row(quantity="0"))
        self.assertTrue(any("większa od zera" in e for e in errors))

    def test_quantity_1_0_is_valid(self):
        self.assertEqual(_validate_row(self._row(quantity="1.0")), [])

    def test_invalid_value_is_error(self):
        errors = _validate_row(self._row(value="nie-liczba"))
        self.assertTrue(any("wartość" in e.lower() for e in errors))

    def test_empty_value_is_valid(self):
        self.assertEqual(_validate_row(self._row(value="")), [])


class ResolveLocationPathTests(TestCase):
    def setUp(self):
        self.root = Location.objects.create(name="ResolveTestRoot")

    def test_creates_single_segment(self):
        loc, new_ids = resolve_location_path(self.root, "Sala A")
        self.assertEqual(loc.name, "Sala A")
        self.assertEqual(loc.parent, self.root)
        self.assertIn(loc.pk, new_ids)

    def test_creates_nested_segments(self):
        loc, new_ids = resolve_location_path(self.root, "Budynek A/Piętro 1/Pokój 12")
        self.assertEqual(loc.name, "Pokój 12")
        self.assertEqual(len(new_ids), 3)

    def test_reuses_existing_location(self):
        existing = Location.objects.create(name="Sala B", parent=self.root)
        loc, new_ids = resolve_location_path(self.root, "Sala B")
        self.assertEqual(loc.pk, existing.pk)
        self.assertNotIn(loc.pk, new_ids)

    def test_partial_create_reuses_existing_parent(self):
        existing = Location.objects.create(name="Budynek B", parent=self.root)
        loc, new_ids = resolve_location_path(self.root, "Budynek B/Pokój 5")
        self.assertEqual(loc.name, "Pokój 5")
        self.assertNotIn(existing.pk, new_ids)
        self.assertIn(loc.pk, new_ids)

    def test_strips_whitespace_from_segments(self):
        loc, _ = resolve_location_path(self.root, " Sala C ")
        self.assertEqual(loc.name, "Sala C")


class ImportAssetsFromRowsTests(TestCase):
    def setUp(self):
        self.root = Location.objects.create(name="ImportTestRoot")
        self.asset_type = AssetTypeDictionary.objects.get(code="fixed")

    def _rows(self, data):
        return [
            {
                "row_number": i + 2,
                "inventory_number": r[0],
                "name": r[1],
                "quantity": r[2],
                "value": r[3],
                "location_path": r[4],
            }
            for i, r in enumerate(data)
        ]

    def test_imports_valid_row(self):
        rows = self._rows([["INW-001", "Laptop A", "1", "", "Sala"]])
        result = import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["error_rows"], [])
        self.assertTrue(Asset.objects.filter(name="Laptop A").exists())

    def test_partial_success_invalid_row_skipped(self):
        rows = self._rows([
            ["INW-001", "Laptop válido", "1", "", "Sala"],
            ["INW-002", "", "1", "", "Sala"],  # brak nazwy
        ])
        result = import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertEqual(result["error_rows"][0]["row"], 3)

    def test_creates_missing_locations(self):
        rows = self._rows([["", "Laptop B", "1", "", "Hala/Pokój 3"]])
        before = Location.objects.count()
        result = import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["created_locations_count"], 2)
        self.assertEqual(Location.objects.count(), before + 2)

    def test_generates_barcode(self):
        rows = self._rows([["", "Laptop C", "1", "", "Sala"]])
        import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        asset = Asset.objects.get(name="Laptop C")
        self.assertNotEqual(asset.barcode, "")

    def test_invalid_quantity_in_row_is_error(self):
        rows = self._rows([["", "Laptop D", "abc", "", "Sala"]])
        result = import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertIn("ilość", result["error_rows"][0]["message"].lower())

    def test_empty_location_in_row_is_error(self):
        rows = self._rows([["", "Laptop E", "1", "", ""]])
        result = import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        self.assertEqual(result["imported_count"], 0)
        self.assertIn("lokalizacji", result["error_rows"][0]["message"].lower())

    def test_records_history_entry(self):
        rows = self._rows([["", "Laptop F", "1", "", "Sala"]])
        import_assets_from_rows(rows, root_location=self.root, asset_type_ref=self.asset_type)
        asset = Asset.objects.get(name="Laptop F")
        self.assertTrue(
            AssetHistoryEntry.objects.filter(
                asset=asset,
                event_type=AssetHistoryEntry.EventType.CREATED,
            ).exists()
        )

    def test_failed_barcode_does_not_create_location(self):
        at_no_prefix = AssetTypeDictionary.objects.create(
            name="TestNoPrefix", code="no-prefix-test", barcode_prefix="", sort_order=99
        )
        rows = self._rows([["", "Laptop G", "1", "", "NowaSalaOrphan"]])
        before = Location.objects.count()
        result = import_assets_from_rows(rows, root_location=self.root, asset_type_ref=at_no_prefix)
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(len(result["error_rows"]), 1)
        self.assertEqual(Location.objects.count(), before)


class AssetImportViewTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user("import_admin", password="pass")
        self.admin_user.profile.role = UserProfile.Role.ADMIN
        self.admin_user.profile.save()
        self.regular_user = User.objects.create_user("import_user", password="pass")
        self.root = Location.objects.create(name="ViewTestRoot")
        self.asset_type = AssetTypeDictionary.objects.get(code="fixed")

    def _import_url(self):
        return reverse("assets:import")

    def _template_url(self):
        return reverse("assets:import-template")

    def test_requires_login(self):
        resp = self.client.get(self._import_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_admin_can_access(self):
        self.client.force_login(self.admin_user)
        resp = self.client.get(self._import_url())
        self.assertEqual(resp.status_code, 200)

    def test_regular_user_is_forbidden(self):
        self.client.force_login(self.regular_user)
        resp = self.client.get(self._import_url())
        self.assertEqual(resp.status_code, 403)

    def test_template_download_returns_xlsx(self):
        self.client.force_login(self.admin_user)
        resp = self.client.get(self._template_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("szablon_importu.xlsx", resp["Content-Disposition"])

    def test_template_download_forbidden_for_user(self):
        self.client.force_login(self.regular_user)
        resp = self.client.get(self._template_url())
        self.assertEqual(resp.status_code, 403)

    def test_import_creates_assets(self):
        self.client.force_login(self.admin_user)
        upload = _make_xlsx_upload([["INW-V1", "Laptop View", "1", "", "Sala"]])
        resp = self.client.post(self._import_url(), {
            "root_location": self.root.pk,
            "asset_type": self.asset_type.pk,
            "import_file": upload,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Asset.objects.filter(name="Laptop View").exists())

    def test_import_creates_locations(self):
        self.client.force_login(self.admin_user)
        before = Location.objects.count()
        upload = _make_xlsx_upload([["", "Monitor View", "1", "", "Nowa Sala"]])
        self.client.post(self._import_url(), {
            "root_location": self.root.pk,
            "asset_type": self.asset_type.pk,
            "import_file": upload,
        })
        self.assertEqual(Location.objects.count(), before + 1)

    def test_import_generates_barcodes(self):
        self.client.force_login(self.admin_user)
        upload = _make_xlsx_upload([["", "Drukarka View", "1", "", "Sala"]])
        self.client.post(self._import_url(), {
            "root_location": self.root.pk,
            "asset_type": self.asset_type.pk,
            "import_file": upload,
        })
        asset = Asset.objects.filter(name="Drukarka View").first()
        self.assertIsNotNone(asset)
        self.assertNotEqual(asset.barcode, "")

    def test_import_report_shows_row_errors(self):
        self.client.force_login(self.admin_user)
        upload = _make_xlsx_upload([
            ["INW-OK", "Klawiatura", "1", "", "Sala"],
            ["INW-ERR", "Mysz", "xyz", "", "Sala"],
        ])
        resp = self.client.post(self._import_url(), {
            "root_location": self.root.pk,
            "asset_type": self.asset_type.pk,
            "import_file": upload,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Niepoprawna ilość")
        self.assertEqual(Asset.objects.filter(name="Klawiatura").count(), 1)
        self.assertEqual(Asset.objects.filter(name="Mysz").count(), 0)


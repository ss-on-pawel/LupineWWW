from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from assets.models import Asset, AssetTypeDictionary
from locations.models import Location
from users.models import User
from .forms import InventorySessionStartForm
from .models import InventorySession, InventoryObservedItem
from .scope import UNASSIGNED_ASSET_TYPE_LABEL
from .services import start_inventory_session, record_mobile_scan


class InventoryUnassignedScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(username="scope-admin")
        cls.root = Location.objects.create(name="Biblioteka")
        cls.child = Location.objects.create(name="Czytelnia", parent=cls.root)
        cls.outside = Location.objects.create(name="Poza zakresem")

    def asset(self, code, kind="", location=None, **kwargs):
        return Asset.objects.create(name=code, barcode=code, asset_type=kind,
                                    location_fk=location or self.child, **kwargs)

    def test_blank_type_is_a_valid_explicit_choice(self):
        form = InventorySessionStartForm(data={"root_locations": [self.root.pk], "asset_types": [""]}, available_locations=Location.objects.filter(pk=self.root.pk))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["asset_types"], [""])
        form = InventorySessionStartForm(data={"root_locations": [self.root.pk]}, available_locations=Location.objects.filter(pk=self.root.pk))
        self.assertFalse(form.is_valid())
        self.assertIn("asset_types", form.errors)

    def test_unassigned_choice_respects_location_archive_and_old_scope(self):
        unassigned = self.asset("NO-TYPE")
        fixed = self.asset("FIXED", "fixed")
        self.asset("OUTSIDE", location=self.outside)
        self.asset("ARCHIVED", status="liquidated", is_active=False)
        old = start_inventory_session(created_by=self.user, root_locations=[self.root], asset_types=["fixed"])
        new = start_inventory_session(created_by=self.user, root_locations=[self.root], asset_types=[""])
        self.assertEqual(list(old.snapshot_items.values_list("asset_id_snapshot", flat=True)), [fixed.pk])
        self.assertEqual(list(new.snapshot_items.values_list("asset_id_snapshot", flat=True)), [unassigned.pk])
        self.assertEqual(new.asset_type_scope, [""])
        self.assertEqual(new.snapshot_items.get().asset_type_display, UNASSIGNED_ASSET_TYPE_LABEL)
        unassigned.refresh_from_db()
        self.assertEqual(unassigned.asset_type, "")

    def test_biblioteka_257_plus_807_equals_1064_without_reclassifying(self):
        Asset.objects.bulk_create([
            Asset(name=f"Środek {i}", barcode=f"BIB-{i}", asset_type="fixed" if i < 257 else "", location_fk=self.child)
            for i in range(1064)
        ])
        narrow = start_inventory_session(created_by=self.user, root_locations=[self.root], asset_types=["fixed"])
        full = start_inventory_session(created_by=self.user, root_locations=[self.root], asset_types=["fixed", ""])
        self.assertEqual(narrow.snapshot_items.count(), 257)
        self.assertEqual(full.snapshot_items.count(), 1064)
        self.assertEqual(Asset.objects.filter(asset_type="").count(), 807)
        self.assertEqual(narrow.snapshot_items.count(), 257)

    def test_unassigned_scan_matches_snapshot_and_reports_show_scope(self):
        asset = self.asset("SCAN-NO-TYPE")
        session = start_inventory_session(created_by=self.user, root_locations=[self.root], asset_types=[""])
        record_mobile_scan(session, asset.barcode, self.child.code)
        self.assertTrue(InventoryObservedItem.objects.filter(session=session, asset=asset).exists())
        self.client.force_login(self.user)
        for route, kwargs in [("inventory:session-list", {}), ("inventory:session-detail", {"pk": session.pk})]:
            response = self.client.get(reverse(route, kwargs=kwargs))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, UNASSIGNED_ASSET_TYPE_LABEL)

    def test_custom_dictionary_type_is_selectable(self):
        kind = AssetTypeDictionary.objects.create(code="custom-books", name="Zbiory specjalne")
        asset = self.asset("CUSTOM-TYPE", kind.code)
        self.client.force_login(self.user)
        response = self.client.get(reverse("inventory:session-start"))
        self.assertContains(response, "Zbiory specjalne")
        response = self.client.post(reverse("inventory:session-start"), {"root_locations": [self.root.pk], "asset_types": [kind.code]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventorySession.objects.get().snapshot_items.get().asset_id_snapshot, asset.pk)

    def test_post_blank_scope_and_select_all_controls(self):
        asset = self.asset("POST-NO-TYPE")
        self.client.force_login(self.user)
        response = self.client.get(reverse("inventory:session-start"))
        self.assertContains(response, 'id="inventory-select-all"')
        self.assertContains(response, 'id="inventory-clear-all"')
        self.assertContains(response, UNASSIGNED_ASSET_TYPE_LABEL)
        response = self.client.post(reverse("inventory:session-start"), {"root_locations": [self.root.pk], "asset_types": [""]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventorySession.objects.get().snapshot_items.get().asset_id_snapshot, asset.pk)

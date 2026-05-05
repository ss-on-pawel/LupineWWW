import json
from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from assets.models import Asset, AssetTypeDictionary
from locations.models import Location
from users.models import User

from .models import (
    InventoryObservedItem,
    InventoryScanBatch,
    InventorySession,
    InventorySessionManualQuantity,
)
from .services import import_inventory_scan_text, start_inventory_session


class StartInventorySessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="inventory-user", password="test-pass-123")
        cls.root = Location.objects.create(name="Root")
        cls.child = Location.objects.create(name="Child", parent=cls.root)
        cls.leaf = Location.objects.create(name="Leaf", parent=cls.child)
        cls.other_root = Location.objects.create(name="Other")

    def _create_asset(self, inventory_number, location, asset_type=Asset.AssetType.FIXED, **overrides):
        defaults = {
            "name": f"Asset {inventory_number}",
            "inventory_number": inventory_number,
            "asset_type": asset_type,
            "barcode": f"BC-{inventory_number}",
            "location_fk": location,
            "location": location.path if location else "Legacy only",
            "status": Asset.Status.IN_STOCK,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def test_session_gets_first_number_and_active_status(self):
        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        self.assertEqual(session.number, "INV-000001")
        self.assertEqual(session.status, InventorySession.Status.ACTIVE)
        self.assertEqual(list(session.scope_root_locations.all()), [self.root])
        self.assertEqual(session.asset_type_scope, [Asset.AssetType.FIXED])

    def test_second_session_gets_next_number(self):
        start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        second_session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        self.assertEqual(second_session.number, "INV-000002")

    def test_snapshot_includes_assets_from_whole_location_subtree(self):
        root_asset = self._create_asset("ROOT-001", self.root)
        child_asset = self._create_asset("CHILD-001", self.child)
        leaf_asset = self._create_asset("LEAF-001", self.leaf)

        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        self.assertCountEqual(
            session.snapshot_items.values_list("asset_id_snapshot", flat=True),
            [root_asset.id, child_asset.id, leaf_asset.id],
        )

    def test_snapshot_filters_by_asset_type(self):
        fixed_asset = self._create_asset("FIXED-001", self.root, Asset.AssetType.FIXED)
        self._create_asset("LOW-001", self.root, Asset.AssetType.LOW_VALUE)

        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        self.assertEqual(session.snapshot_items.count(), 1)
        self.assertEqual(session.snapshot_items.get().asset_id_snapshot, fixed_asset.id)

    def test_snapshot_does_not_change_after_asset_update(self):
        asset = self._create_asset("SNAP-001", self.root, name="Original name", barcode="BC-ORIGINAL")

        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )
        snapshot = session.snapshot_items.get()

        asset.name = "Changed name"
        asset.barcode = "BC-CHANGED"
        asset.status = Asset.Status.IN_USE
        asset.save(update_fields=["name", "barcode", "status", "updated_at"])
        snapshot.refresh_from_db()

        self.assertEqual(snapshot.name, "Original name")
        self.assertEqual(snapshot.barcode, "BC-ORIGINAL")
        self.assertEqual(snapshot.status_snapshot, Asset.Status.IN_STOCK)

    def test_asset_without_location_fk_is_not_snapshotted(self):
        self._create_asset("NOLOC-001", None)

        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        self.assertEqual(session.snapshot_items.count(), 0)

    def test_asset_outside_subtree_is_not_snapshotted(self):
        in_scope_asset = self._create_asset("IN-001", self.child)
        self._create_asset("OUT-001", self.other_root)

        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

        self.assertEqual(session.snapshot_items.count(), 1)
        self.assertEqual(session.snapshot_items.get().asset_id_snapshot, in_scope_asset.id)


class InventorySessionListViewTests(TestCase):
    def setUp(self):
        self.root = Location.objects.create(name="List Root")
        self.child = Location.objects.create(name="List Child", parent=self.root)
        self.other_root = Location.objects.create(name="List Other")
        self.admin_user = User.objects.create_superuser(
            username="inventory-list-admin",
            email="inventory-list-admin@example.com",
            password="test-pass-123",
        )
        self.profile_admin_user = User.objects.create_user(username="inventory-profile-admin", password="test-pass-123")
        self.profile_admin_user.profile.role = UserProfile.Role.ADMIN
        self.profile_admin_user.profile.save(update_fields=["role"])
        self.scoped_user = User.objects.create_user(username="inventory-list-user", password="test-pass-123")
        self.scoped_user.profile.allowed_locations.add(self.root)
        self.child_scoped_user = User.objects.create_user(username="inventory-list-child", password="test-pass-123")
        self.child_scoped_user.profile.allowed_locations.add(self.child)
        self.no_access_user = User.objects.create_user(username="inventory-list-empty", password="test-pass-123")
        self.asset = self._create_asset("LIST-IN-001", self.child)
        self.other_asset = self._create_asset("LIST-OUT-001", self.other_root)

    def _create_asset(self, inventory_number, location, asset_type=Asset.AssetType.FIXED):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=asset_type,
            barcode=f"BC-{inventory_number}",
            location=location.path,
            location_fk=location,
            status=Asset.Status.IN_STOCK,
        )

    def _start_session(self, root_location):
        return start_inventory_session(
            created_by=self.admin_user,
            root_locations=[root_location],
            asset_types=[Asset.AssetType.FIXED],
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_superuser_sees_all_sessions(self):
        in_scope_session = self._start_session(self.root)
        out_of_scope_session = self._start_session(self.other_root)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, in_scope_session.number)
        self.assertContains(response, out_of_scope_session.number)

    def test_profile_admin_sees_all_sessions(self):
        in_scope_session = self._start_session(self.root)
        out_of_scope_session = self._start_session(self.other_root)
        self.client.force_login(self.profile_admin_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, in_scope_session.number)
        self.assertContains(response, out_of_scope_session.number)

    def test_user_sees_session_overlapping_allowed_scope(self):
        session = self._start_session(self.root)
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, session.number)

    def test_user_sees_session_when_allowed_child_overlaps_session_subtree(self):
        session = self._start_session(self.root)
        self.client.force_login(self.child_scoped_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, session.number)

    def test_user_does_not_see_session_outside_allowed_scope(self):
        session = self._start_session(self.other_root)
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, session.number)

    def test_user_without_allowed_locations_sees_empty_list(self):
        session = self._start_session(self.root)
        self.client.force_login(self.no_access_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, session.number)
        self.assertContains(response, "Brak sesji inwentaryzacji")

    def test_page_shows_session_number_and_snapshot_item_count(self):
        session = self._start_session(self.root)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("inventory:session-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, session.number)
        self.assertContains(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        self.assertContains(response, "<td>1</td>", html=True)


class InventorySessionDetailViewTests(TestCase):
    def setUp(self):
        self.root = Location.objects.create(name="Detail Root")
        self.child = Location.objects.create(name="Detail Child", parent=self.root)
        self.other_root = Location.objects.create(name="Detail Other")
        self.admin_user = User.objects.create_superuser(
            username="inventory-detail-admin",
            email="inventory-detail-admin@example.com",
            password="test-pass-123",
        )
        self.scoped_user = User.objects.create_user(username="inventory-detail-user", password="test-pass-123")
        self.scoped_user.profile.allowed_locations.add(self.root)
        self.out_of_scope_user = User.objects.create_user(username="inventory-detail-out", password="test-pass-123")
        self.out_of_scope_user.profile.allowed_locations.add(self.other_root)

    def _create_asset(self, inventory_number, location, **overrides):
        defaults = {
            "name": f"Asset {inventory_number}",
            "inventory_number": inventory_number,
            "asset_type": Asset.AssetType.FIXED,
            "barcode": f"BC-{inventory_number}",
            "location": location.path,
            "location_fk": location,
            "status": Asset.Status.IN_STOCK,
        }
        defaults.update(overrides)
        return Asset.objects.create(**defaults)

    def _start_session(self, root_location):
        return start_inventory_session(
            created_by=self.admin_user,
            root_locations=[root_location],
            asset_types=[Asset.AssetType.FIXED],
        )

    def test_anonymous_user_is_redirected_to_login(self):
        session = self._start_session(self.root)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_superuser_sees_any_session_detail(self):
        session = self._start_session(self.root)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, session.number)

    def test_user_sees_session_detail_in_allowed_scope(self):
        session = self._start_session(self.root)
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, session.number)

    def test_user_does_not_see_session_detail_outside_allowed_scope(self):
        session = self._start_session(self.root)
        self.client.force_login(self.out_of_scope_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 404)

    def test_detail_shows_snapshot_items(self):
        asset = self._create_asset("DETAIL-001", self.child, name="Detail Asset", barcode="BC-DETAIL")
        session = self._start_session(self.root)
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, session.number)
        self.assertContains(response, asset.inventory_number)
        self.assertContains(response, "Detail Asset")
        self.assertContains(response, "Środek trwały")
        self.assertContains(response, "Detail Root / Detail Child")
        self.assertContains(response, "BC-DETAIL")

    def test_detail_uses_snapshot_data_after_asset_update(self):
        asset = self._create_asset("DETAIL-SNAP-001", self.child, name="Snapshot Name", barcode="BC-SNAPSHOT")
        session = self._start_session(self.root)

        asset.name = "Current Name"
        asset.location_fk = self.other_root
        asset.location = self.other_root.path
        asset.barcode = "BC-CURRENT"
        asset.save(update_fields=["name", "location_fk", "location", "barcode", "updated_at"])
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Snapshot Name")
        self.assertContains(response, "Detail Root / Detail Child")
        self.assertContains(response, "BC-SNAPSHOT")
        self.assertNotContains(response, "Current Name")
        self.assertNotContains(response, "Detail Other")
        self.assertNotContains(response, "BC-CURRENT")


class InventorySessionStartViewTests(TestCase):
    def setUp(self):
        self.root = Location.objects.create(name="Start Root")
        self.child = Location.objects.create(name="Start Child", parent=self.root)
        self.other_root = Location.objects.create(name="Start Other")
        self.admin_user = User.objects.create_superuser(
            username="inventory-start-admin",
            email="inventory-start-admin@example.com",
            password="test-pass-123",
        )
        self.manager_user = User.objects.create_user(username="inventory-start-manager", password="test-pass-123")
        self.manager_user.profile.role = UserProfile.Role.MANAGER
        self.manager_user.profile.save(update_fields=["role"])
        self.manager_user.profile.allowed_locations.add(self.root)
        self.user = User.objects.create_user(username="inventory-start-user", password="test-pass-123")
        self.user.profile.allowed_locations.add(self.root)
        self.no_access_user = User.objects.create_user(username="inventory-start-empty", password="test-pass-123")
        self._create_asset("START-FIXED-001", self.child, Asset.AssetType.FIXED)
        self._create_asset("START-LOW-001", self.child, Asset.AssetType.LOW_VALUE)
        self._create_asset("START-OTHER-001", self.other_root, Asset.AssetType.FIXED)

    def _create_asset(self, inventory_number, location, asset_type):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=asset_type,
            barcode=f"BC-{inventory_number}",
            location=location.path,
            location_fk=location,
            status=Asset.Status.IN_STOCK,
        )

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("inventory:session-start"))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_regular_user_get_sees_simple_confirmation(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:session-start"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rozpoczniesz nową inwentaryzację w swoim dostępnym zakresie")
        self.assertContains(response, "Start Root")
        self.assertNotContains(response, "name=\"root_locations\"")
        self.assertNotContains(response, "name=\"asset_types\"")

    def test_regular_user_post_creates_session_in_allowed_locations(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse("inventory:session-start"))

        session = InventorySession.objects.get()
        self.assertRedirects(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        self.assertEqual(session.number, "INV-000001")
        self.assertEqual(session.asset_type_scope, [Asset.AssetType.FIXED, Asset.AssetType.LOW_VALUE])
        self.assertEqual(list(session.scope_root_locations.all()), [self.root])
        self.assertEqual(session.snapshot_items.count(), 2)

    def test_regular_user_without_allowed_locations_does_not_create_session(self):
        self.client.force_login(self.no_access_user)

        response = self.client.post(reverse("inventory:session-start"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InventorySession.objects.count(), 0)
        self.assertContains(response, "Nie masz przypisanych lokalizacji")

    def test_regular_user_cannot_force_location_outside_scope(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("inventory:session-start"),
            {
                "root_locations": [str(self.other_root.id)],
                "asset_types": [Asset.AssetType.FIXED],
            },
        )

        session = InventorySession.objects.get()
        self.assertRedirects(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        self.assertEqual(list(session.scope_root_locations.all()), [self.root])
        self.assertNotIn(self.other_root.id, session.scope_root_locations.values_list("id", flat=True))

    def test_manager_get_sees_location_and_asset_type_checkboxes(self):
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse("inventory:session-start"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "name=\"root_locations\"")
        self.assertContains(response, "name=\"asset_types\"")
        self.assertContains(response, "Start Root")
        self.assertContains(response, "Środek trwały")
        self.assertContains(response, "Niskocenny")
        self.assertNotContains(response, "Start Other")

    def test_manager_post_creates_session_for_selected_locations(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("inventory:session-start"),
            {
                "root_locations": [str(self.root.id)],
                "asset_types": [Asset.AssetType.FIXED],
            },
        )

        session = InventorySession.objects.get()
        self.assertRedirects(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        self.assertEqual(list(session.scope_root_locations.all()), [self.root])
        self.assertEqual(session.asset_type_scope, [Asset.AssetType.FIXED])
        self.assertEqual(session.snapshot_items.count(), 1)

    def test_manager_cannot_create_session_outside_scope(self):
        self.client.force_login(self.manager_user)

        response = self.client.post(
            reverse("inventory:session-start"),
            {
                "root_locations": [str(self.other_root.id)],
                "asset_types": [Asset.AssetType.FIXED],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(InventorySession.objects.count(), 0)
        self.assertContains(response, "Wybierz poprawną wartość")

    def test_superuser_can_create_session_for_root_location(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("inventory:session-start"),
            {
                "root_locations": [str(self.other_root.id)],
                "asset_types": [Asset.AssetType.FIXED],
            },
        )

        session = InventorySession.objects.get()
        self.assertRedirects(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        self.assertEqual(list(session.scope_root_locations.all()), [self.other_root])
        self.assertEqual(session.snapshot_items.count(), 1)


class InventorySessionCloseViewTests(TestCase):
    def setUp(self):
        self.root = Location.objects.create(name="Close Root")
        self.child = Location.objects.create(name="Close Child", parent=self.root)
        self.other_root = Location.objects.create(name="Close Other")
        self.admin_user = User.objects.create_superuser(
            username="inventory-close-admin",
            email="inventory-close-admin@example.com",
            password="test-pass-123",
        )
        self.scoped_user = User.objects.create_user(username="inventory-close-user", password="test-pass-123")
        self.scoped_user.profile.allowed_locations.add(self.root)
        self.out_of_scope_user = User.objects.create_user(username="inventory-close-out", password="test-pass-123")
        self.out_of_scope_user.profile.allowed_locations.add(self.other_root)
        self._create_asset("CLOSE-001", self.child)

    def _create_asset(self, inventory_number, location):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=Asset.AssetType.FIXED,
            barcode=f"BC-{inventory_number}",
            location=location.path,
            location_fk=location,
            status=Asset.Status.IN_STOCK,
        )

    def _start_session(self):
        return start_inventory_session(
            created_by=self.admin_user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

    def test_anonymous_post_is_redirected_to_login(self):
        session = self._start_session()

        response = self.client.post(reverse("inventory:session-close", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_user_in_scope_can_close_active_session(self):
        session = self._start_session()
        self.client.force_login(self.scoped_user)

        response = self.client.post(reverse("inventory:session-close", kwargs={"pk": session.pk}))

        self.assertRedirects(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.Status.CLOSED)
        self.assertIsNotNone(session.closed_at)

    def test_user_outside_scope_cannot_close_session(self):
        session = self._start_session()
        self.client.force_login(self.out_of_scope_user)

        response = self.client.post(reverse("inventory:session-close", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 404)
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.Status.ACTIVE)
        self.assertIsNone(session.closed_at)

    def test_closing_closed_session_does_not_change_closed_at(self):
        session = self._start_session()
        closed_at = timezone.now()
        session.status = InventorySession.Status.CLOSED
        session.closed_at = closed_at
        session.save(update_fields=["status", "closed_at", "updated_at"])
        self.client.force_login(self.scoped_user)

        response = self.client.post(reverse("inventory:session-close", kwargs={"pk": session.pk}))

        self.assertRedirects(response, reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        session.refresh_from_db()
        self.assertEqual(session.status, InventorySession.Status.CLOSED)
        self.assertEqual(session.closed_at, closed_at)

    def test_close_button_is_visible_for_active_session(self):
        session = self._start_session()
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zamknij inwentaryzację")
        self.assertContains(response, reverse("inventory:session-close", kwargs={"pk": session.pk}))

    def test_close_button_is_hidden_for_closed_session(self):
        session = self._start_session()
        session.status = InventorySession.Status.CLOSED
        session.closed_at = timezone.now()
        session.save(update_fields=["status", "closed_at", "updated_at"])
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Zamknij inwentaryzację")
        self.assertNotContains(response, reverse("inventory:session-close", kwargs={"pk": session.pk}))


class ImportInventoryScanTextTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="inventory-import-user", password="test-pass-123")
        self.root = Location.objects.create(name="Import Root")
        self.child = Location.objects.create(name="Import Child", parent=self.root)
        self.other_location = Location.objects.create(name="Import Other")
        self.in_scope_asset = self._create_asset("IMPORT-IN-001", self.child, barcode="BC-IMPORT-IN")
        self.other_location_asset = self._create_asset("IMPORT-OTHERLOC-001", self.child, barcode="BC-IMPORT-OTHERLOC")
        self.out_of_scope_asset = self._create_asset("IMPORT-OUT-001", self.other_location, barcode="BC-IMPORT-OUT")
        self.session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

    def _create_asset(self, inventory_number, location, barcode="", asset_type=Asset.AssetType.FIXED):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=asset_type,
            barcode=barcode,
            location=location.path,
            location_fk=location,
            status=Asset.Status.IN_STOCK,
        )

    def test_import_resolves_session_from_first_non_empty_line(self):
        batch = import_inventory_scan_text(
            f"\n\n{self.session.number}\n{self.child.code}\nBC-IMPORT-IN\n",
            uploaded_by=self.user,
        )

        self.assertEqual(batch.session, self.session)
        self.assertEqual(batch.uploaded_by, self.user)

    def test_missing_session_raises_value_error(self):
        with self.assertRaises(ValueError):
            import_inventory_scan_text("INV-999999\nBC-IMPORT-IN")

    def test_closed_session_raises_value_error(self):
        self.session.status = InventorySession.Status.CLOSED
        self.session.closed_at = timezone.now()
        self.session.save(update_fields=["status", "closed_at", "updated_at"])

        with self.assertRaises(ValueError):
            import_inventory_scan_text(f"{self.session.number}\nBC-IMPORT-IN")

    def test_location_code_sets_current_location(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nBC-IMPORT-IN")

        observed = InventoryObservedItem.objects.get(asset=self.in_scope_asset)
        self.assertEqual(observed.scanned_location, self.child)

    def test_asset_in_snapshot_location_is_found_ok(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nBC-IMPORT-IN")

        observed = InventoryObservedItem.objects.get(asset=self.in_scope_asset)
        self.assertEqual(observed.status, InventoryObservedItem.Status.FOUND_OK)

    def test_asset_in_snapshot_other_location_is_found_other_location(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.root.code}\nBC-IMPORT-OTHERLOC")

        observed = InventoryObservedItem.objects.get(asset=self.other_location_asset)
        self.assertEqual(observed.status, InventoryObservedItem.Status.FOUND_OTHER_LOCATION)

    def test_asset_outside_snapshot_is_found_out_of_scope(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.other_location.code}\nBC-IMPORT-OUT")

        observed = InventoryObservedItem.objects.get(asset=self.out_of_scope_asset)
        self.assertEqual(observed.status, InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE)

    def test_barcode_has_priority_over_inventory_number(self):
        barcode_asset = self._create_asset("IMPORT-BARCODE-ASSET", self.child, barcode="IMPORT-CONFLICT-001")
        inventory_number_asset = self._create_asset("IMPORT-CONFLICT-001", self.child, barcode="BC-CONFLICT-INVENTORY")

        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nIMPORT-CONFLICT-001")

        self.assertTrue(InventoryObservedItem.objects.filter(asset=barcode_asset).exists())
        self.assertFalse(InventoryObservedItem.objects.filter(asset=inventory_number_asset).exists())

    def test_inventory_number_fallback_works(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nIMPORT-IN-001")

        observed = InventoryObservedItem.objects.get(asset=self.in_scope_asset)
        self.assertEqual(observed.code, "IMPORT-IN-001")
        self.assertEqual(observed.status, InventoryObservedItem.Status.FOUND_OK)

    def test_multiple_scans_of_same_asset_keep_one_observed_item(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nBC-IMPORT-IN\nBC-IMPORT-IN")

        self.assertEqual(InventoryObservedItem.objects.filter(asset=self.in_scope_asset).count(), 1)

    def test_rescan_updates_location_status_and_last_seen_at(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.root.code}\nBC-IMPORT-IN")
        observed = InventoryObservedItem.objects.get(asset=self.in_scope_asset)
        first_seen_at = observed.first_seen_at
        first_last_seen_at = observed.last_seen_at

        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nBC-IMPORT-IN")

        observed.refresh_from_db()
        self.assertEqual(observed.scanned_location, self.child)
        self.assertEqual(observed.status, InventoryObservedItem.Status.FOUND_OK)
        self.assertEqual(observed.first_seen_at, first_seen_at)
        self.assertGreaterEqual(observed.last_seen_at, first_last_seen_at)

    def test_unknown_code_creates_unknown_observed_item(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nUNKNOWN-CODE-001")

        observed = InventoryObservedItem.objects.get(asset__isnull=True, code="UNKNOWN-CODE-001")
        self.assertEqual(observed.status, InventoryObservedItem.Status.UNKNOWN_CODE)
        self.assertEqual(observed.scanned_location, self.child)

    def test_scan_batch_stores_raw_text_and_counters(self):
        raw_text = f"{self.session.number}\n{self.child.code}\nBC-IMPORT-IN\nUNKNOWN-CODE-002\n"

        batch = import_inventory_scan_text(raw_text, uploaded_by=self.user)

        self.assertEqual(InventoryScanBatch.objects.count(), 1)
        self.assertEqual(batch.raw_text, raw_text)
        self.assertEqual(batch.total_lines, 4)
        self.assertEqual(batch.processed_lines, 3)
        self.assertEqual(batch.recognized_assets_count, 1)
        self.assertEqual(batch.unknown_codes_count, 1)
        self.assertIsNotNone(batch.processed_at)


class InventorySessionDetailScanProgressTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="inventory-progress-admin",
            email="inventory-progress-admin@example.com",
            password="test-pass-123",
        )
        self.root = Location.objects.create(name="Progress Root")
        self.child = Location.objects.create(name="Progress Child", parent=self.root)
        self.other_location = Location.objects.create(name="Progress Other")
        self.ok_asset = self._create_asset("PROGRESS-OK-001", self.child, barcode="BC-PROGRESS-OK")
        self.other_asset = self._create_asset("PROGRESS-OTHER-001", self.child, barcode="BC-PROGRESS-OTHER")
        self.out_asset = self._create_asset("PROGRESS-OUT-001", self.other_location, barcode="BC-PROGRESS-OUT")
        self.session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )

    def _create_asset(self, inventory_number, location, barcode, asset_type=Asset.AssetType.FIXED):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=asset_type,
            barcode=barcode,
            location=location.path,
            location_fk=location,
            status=Asset.Status.IN_STOCK,
        )

    def _detail_response(self):
        self.client.force_login(self.user)
        return self.client.get(reverse("inventory:session-detail", kwargs={"pk": self.session.pk}))

    def test_detail_shows_inventory_progress_section(self):
        response = self._detail_response()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Postęp inwentaryzacji")
        self.assertContains(response, "Składniki inwentaryzacji")

    def test_progress_counters_show_observed_status_counts(self):
        import_inventory_scan_text(
            "\n".join(
                [
                    self.session.number,
                    self.child.code,
                    "BC-PROGRESS-OK",
                    self.root.code,
                    "BC-PROGRESS-OTHER",
                    self.other_location.code,
                    "BC-PROGRESS-OUT",
                    "UNKNOWN-PROGRESS-001",
                ]
            )
        )

        response = self._detail_response()

        self.assertContains(response, "Odczytano")
        self.assertContains(response, "3 / 2")
        self.assertContains(response, "Zgodne")
        self.assertContains(response, "Inna lokalizacja")
        self.assertContains(response, "Poza zakresem")
        self.assertContains(response, "Nieznane kody")
        self.assertContains(response, "<dd>1</dd>", html=True)

    def test_work_table_shows_snapshot_item_without_scan_as_missing(self):
        response = self._detail_response()

        self.assertContains(response, "Składniki inwentaryzacji")
        self.assertContains(response, "PROGRESS-OK-001")
        self.assertContains(response, "Brak odczytu")

    def test_work_table_shows_found_ok_status_location_and_last_seen(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nBC-PROGRESS-OK")
        observed = InventoryObservedItem.objects.get(asset=self.ok_asset)
        last_seen_display = timezone.localtime(observed.last_seen_at).strftime("%Y-%m-%d %H:%M")

        response = self._detail_response()

        self.assertContains(response, "Składniki inwentaryzacji")
        self.assertContains(response, "BC-PROGRESS-OK")
        self.assertContains(response, "PROGRESS-OK-001")
        self.assertContains(response, "Asset PROGRESS-OK-001")
        self.assertContains(response, "Zgodne")
        self.assertContains(response, "Progress Root / Progress Child")
        self.assertContains(response, last_seen_display)

    def test_work_table_shows_operational_quantity_columns(self):
        response = self._detail_response()

        self.assertContains(response, "Odczyt")
        self.assertContains(response, "Kod kreskowy")
        self.assertContains(response, "Ręczne")
        self.assertContains(response, "Ilość faktyczna")

    def test_regular_asset_is_not_quantity_based_and_read_is_binary(self):
        import_inventory_scan_text(
            "\n".join(
                [
                    self.session.number,
                    self.child.code,
                    "BC-PROGRESS-OK",
                    "BC-PROGRESS-OK",
                ]
            )
        )

        response = self._detail_response()
        work_item = next(
            item for item in response.context["inventory_work_items"]
            if item["snapshot"].inventory_number == "PROGRESS-OK-001"
        )

        self.assertFalse(work_item["is_quantity_based"])
        self.assertEqual(work_item["read_quantity"], 1)
        self.assertEqual(work_item["actual_quantity"], 1)

    def test_work_table_context_includes_asset_record_quantity(self):
        self.ok_asset.record_quantity = 42
        self.ok_asset.save(update_fields=["record_quantity"])

        response = self._detail_response()
        work_item = next(
            item for item in response.context["inventory_work_items"]
            if item["snapshot"].inventory_number == "PROGRESS-OK-001"
        )

        self.assertEqual(work_item["record_quantity"], 42)

    def test_quantity_asset_read_is_sum_of_scan_occurrences(self):
        quantity_type = AssetTypeDictionary.objects.get(code=Asset.AssetType.QUANTITY)
        quantity_asset = self._create_asset(
            "PROGRESS-QTY-001",
            self.child,
            barcode="BC-PROGRESS-QTY",
            asset_type=Asset.AssetType.QUANTITY,
        )
        self.assertEqual(quantity_asset.asset_type_ref, quantity_type)
        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.QUANTITY],
        )
        import_inventory_scan_text(
            "\n".join(
                [
                    session.number,
                    self.child.code,
                    "BC-PROGRESS-QTY",
                    "BC-PROGRESS-QTY",
                    "BC-PROGRESS-QTY",
                ]
            )
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        work_item = response.context["inventory_work_items"][0]

        self.assertTrue(work_item["is_quantity_based"])
        self.assertEqual(work_item["read_quantity"], 3)
        self.assertEqual(work_item["actual_quantity"], 3)

    def test_quantity_asset_read_uses_exact_scan_line_matches(self):
        self._create_asset(
            "TEST-QTY-0001",
            self.child,
            barcode="TEST-QTY-0001",
            asset_type=Asset.AssetType.QUANTITY,
        )
        self._create_asset(
            "TEST-QTY-001",
            self.child,
            barcode="TEST-QTY-001",
            asset_type=Asset.AssetType.QUANTITY,
        )
        self._create_asset(
            "TEST-QTY-0010",
            self.child,
            barcode="TEST-QTY-0010",
            asset_type=Asset.AssetType.QUANTITY,
        )
        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.QUANTITY],
        )
        import_inventory_scan_text(
            "\n".join(
                [
                    session.number,
                    self.child.code,
                    " TEST-QTY-0001 ",
                    "TEST-QTY-0010",
                    "TEST-QTY-0001",
                ]
            )
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        work_items = {
            item["snapshot"].inventory_number: item
            for item in response.context["inventory_work_items"]
        }

        self.assertEqual(work_items["TEST-QTY-0001"]["read_quantity"], 2)
        self.assertEqual(work_items["TEST-QTY-001"]["read_quantity"], 0)
        self.assertEqual(work_items["TEST-QTY-0010"]["read_quantity"], 1)

    def test_work_table_uses_saved_manual_quantity(self):
        quantity_asset = self._create_asset(
            "PROGRESS-MANUAL-001",
            self.child,
            barcode="BC-PROGRESS-MANUAL",
            asset_type=Asset.AssetType.QUANTITY,
        )
        session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.QUANTITY],
        )
        import_inventory_scan_text(f"{session.number}\n{self.child.code}\nBC-PROGRESS-MANUAL")
        InventorySessionManualQuantity.objects.create(
            session=session,
            asset=quantity_asset,
            quantity=4,
            updated_by=self.user,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:session-detail", kwargs={"pk": session.pk}))
        work_item = response.context["inventory_work_items"][0]

        self.assertEqual(work_item["manual_quantity"], 4)
        self.assertEqual(work_item["read_quantity"], 1)
        self.assertEqual(work_item["actual_quantity"], 5)
        self.assertContains(response, 'value="4"')

    def test_work_table_shows_found_other_location(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.root.code}\nBC-PROGRESS-OTHER")

        response = self._detail_response()

        self.assertContains(response, "PROGRESS-OTHER-001")
        self.assertContains(response, "Inna lokalizacja")
        self.assertContains(response, "Progress Root")

    def test_unknown_code_is_shown_in_problem_section(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nUNKNOWN-PROGRESS-002")

        response = self._detail_response()

        self.assertContains(response, "Problemy i odczyty spoza ewidencji")
        self.assertContains(response, "UNKNOWN-PROGRESS-002")
        self.assertContains(response, "Nieznany kod")

    def test_found_out_of_scope_is_shown_in_problem_section(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.other_location.code}\nBC-PROGRESS-OUT")

        response = self._detail_response()

        self.assertContains(response, "Problemy i odczyty spoza ewidencji")
        self.assertContains(response, "BC-PROGRESS-OUT")
        self.assertContains(response, "Poza zakresem")
        self.assertContains(response, "Progress Other")

    def test_scan_imports_list_shows_batch_counters(self):
        import_inventory_scan_text(f"{self.session.number}\n{self.child.code}\nBC-PROGRESS-OK\nUNKNOWN-PROGRESS-003")

        response = self._detail_response()

        self.assertContains(response, "Importy skanów")
        self.assertContains(response, "<td>4</td>", html=True)
        self.assertContains(response, "<td>3</td>", html=True)
        self.assertContains(response, "<td>1</td>", html=True)

    def test_empty_problem_state_is_shown(self):
        response = self._detail_response()

        self.assertContains(response, "Brak problemów poza ewidencją.")

    def test_empty_imports_state_is_shown(self):
        response = self._detail_response()

        self.assertContains(response, "Brak importów skanów.")

    def test_snapshot_items_are_visible_in_work_table(self):
        response = self._detail_response()

        self.assertNotContains(response, "Snapshot startowy")
        self.assertContains(response, "PROGRESS-OK-001")
        self.assertContains(response, "PROGRESS-OTHER-001")


class InventorySessionManualQuantityModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username="manual-model-admin",
            email="manual-model-admin@example.com",
            password="test-pass-123",
        )
        self.root = Location.objects.create(name="Manual Model Root")
        self.asset = Asset.objects.create(
            name="Manual Model Asset",
            inventory_number="MANUAL-MODEL-001",
            asset_type=Asset.AssetType.QUANTITY,
            barcode="BC-MANUAL-MODEL",
            location=self.root.path,
            location_fk=self.root,
            status=Asset.Status.IN_STOCK,
        )
        self.session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.QUANTITY],
        )

    def test_can_store_manual_quantity_for_session_asset(self):
        manual_quantity = InventorySessionManualQuantity.objects.create(
            session=self.session,
            asset=self.asset,
            quantity=5,
            updated_by=self.user,
        )

        self.assertEqual(manual_quantity.quantity, 5)
        self.assertEqual(manual_quantity.session, self.session)
        self.assertEqual(manual_quantity.asset, self.asset)

    def test_unique_constraint_prevents_duplicate_session_asset_quantity(self):
        InventorySessionManualQuantity.objects.create(
            session=self.session,
            asset=self.asset,
            quantity=1,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InventorySessionManualQuantity.objects.create(
                    session=self.session,
                    asset=self.asset,
                    quantity=2,
                )


class InventoryManualQuantityApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="manual-api-user", password="test-pass-123")
        self.root = Location.objects.create(name="Manual API Root")
        self.child = Location.objects.create(name="Manual API Child", parent=self.root)
        self.other_root = Location.objects.create(name="Manual API Other")
        self.user.profile.allowed_locations.add(self.root)
        self.quantity_asset = self._create_asset(
            "MANUAL-QTY-001",
            self.child,
            barcode="BC-MANUAL-QTY",
            asset_type=Asset.AssetType.QUANTITY,
        )
        self.fixed_asset = self._create_asset(
            "MANUAL-FIXED-001",
            self.child,
            barcode="BC-MANUAL-FIXED",
            asset_type=Asset.AssetType.FIXED,
        )
        self.session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.QUANTITY, Asset.AssetType.FIXED],
        )
        self.url = reverse("inventory:manual-quantity-api", kwargs={"session_id": self.session.pk})

    def _create_asset(self, inventory_number, location, barcode, asset_type):
        return Asset.objects.create(
            name=f"Asset {inventory_number}",
            inventory_number=inventory_number,
            asset_type=asset_type,
            barcode=barcode,
            location=location.path,
            location_fk=location,
            status=Asset.Status.IN_STOCK,
        )

    def _post(self, payload):
        self.client.force_login(self.user)
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_logged_in_user_can_save_quantity(self):
        response = self._post({"asset_id": self.quantity_asset.id, "quantity": 5})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["asset_id"], self.quantity_asset.id)
        self.assertEqual(payload["manual_quantity"], 5)
        self.assertEqual(payload["actual_quantity"], 5)
        self.assertEqual(
            InventorySessionManualQuantity.objects.get(session=self.session, asset=self.quantity_asset).quantity,
            5,
        )

    def test_second_post_updates_existing_quantity(self):
        self._post({"asset_id": self.quantity_asset.id, "quantity": 5})

        response = self._post({"asset_id": self.quantity_asset.id, "quantity": 2})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["manual_quantity"], 2)
        self.assertEqual(InventorySessionManualQuantity.objects.count(), 1)

    def test_blank_quantity_is_saved_as_zero(self):
        response = self._post({"asset_id": self.quantity_asset.id, "quantity": ""})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["manual_quantity"], 0)

    def test_negative_quantity_returns_400(self):
        response = self._post({"asset_id": self.quantity_asset.id, "quantity": -1})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(InventorySessionManualQuantity.objects.exists())

    def test_non_numeric_quantity_returns_400(self):
        response = self._post({"asset_id": self.quantity_asset.id, "quantity": "abc"})

        self.assertEqual(response.status_code, 400)

    def test_missing_asset_id_returns_400(self):
        response = self._post({"quantity": 3})

        self.assertEqual(response.status_code, 400)

    def test_unknown_asset_id_returns_404(self):
        response = self._post({"asset_id": 999999, "quantity": 3})

        self.assertEqual(response.status_code, 404)

    def test_regular_asset_rejects_manual_quantity(self):
        response = self._post({"asset_id": self.fixed_asset.id, "quantity": 3})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            InventorySessionManualQuantity.objects.filter(session=self.session, asset=self.fixed_asset).exists()
        )


class SeedInventoryDemoCommandTests(TestCase):
    def _run_command(self):
        stdout = StringIO()
        call_command("seed_inventory_demo", stdout=stdout)
        return stdout.getvalue()

    def test_command_creates_demo_inventory_data(self):
        output = self._run_command()

        session = InventorySession.objects.get(number="INV-DEMO-001")
        self.assertIn("Seed demo inwentaryzacji zakończony", output)
        self.assertEqual(session.scope_root_locations.count(), 2)
        self.assertEqual(Asset.objects.filter(inventory_number__startswith="TEST-AST-").count(), 30)
        self.assertEqual(Asset.objects.filter(inventory_number__startswith="TEST-QTY-").count(), 10)
        self.assertEqual(session.snapshot_items.count(), 40)
        self.assertEqual(InventoryScanBatch.objects.filter(session=session).count(), 1)
        self.assertFalse(
            Asset.objects
            .filter(inventory_number__startswith="TEST-QTY-", record_quantity__lte=1)
            .exists()
        )
        self.assertTrue(
            InventoryScanBatch.objects
            .filter(session=session, raw_text__contains="UNKNOWN-DEMO-001")
            .exists()
        )
        self.assertTrue(
            InventoryScanBatch.objects
            .filter(session=session, raw_text__contains="TEST-QTY-0010")
            .exists()
        )
        self.assertEqual(InventorySessionManualQuantity.objects.filter(session=session).count(), 4)

    def test_command_can_be_run_twice_without_uncontrolled_duplicates(self):
        self._run_command()
        self._run_command()

        session = InventorySession.objects.get(number="INV-DEMO-001")
        self.assertEqual(InventorySession.objects.filter(number="INV-DEMO-001").count(), 1)
        self.assertEqual(Asset.objects.filter(inventory_number__startswith="TEST-AST-").count(), 30)
        self.assertEqual(Asset.objects.filter(inventory_number__startswith="TEST-QTY-").count(), 10)
        self.assertEqual(InventoryScanBatch.objects.filter(session=session).count(), 1)
        self.assertEqual(InventorySessionManualQuantity.objects.filter(session=session).count(), 4)


class ScanFileImportApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="scan-api-user", password="test-pass-123")
        self.root = Location.objects.create(name="API Root")
        self.child = Location.objects.create(name="API Child", parent=self.root)
        self.other_root = Location.objects.create(name="API Other Root")
        self.other_child = Location.objects.create(name="API Other Child", parent=self.other_root)
        self.user.profile.allowed_locations.add(self.root)
        self.out_of_scope_user = User.objects.create_user(username="scan-api-out-user", password="test-pass-123")
        self.out_of_scope_user.profile.allowed_locations.add(self.other_root)
        self.manager_user = User.objects.create_user(username="scan-api-manager", password="test-pass-123")
        self.manager_user.profile.role = UserProfile.Role.MANAGER
        self.manager_user.profile.save(update_fields=["role"])
        self.manager_user.profile.allowed_locations.add(self.root)
        self.out_of_scope_manager = User.objects.create_user(username="scan-api-out-manager", password="test-pass-123")
        self.out_of_scope_manager.profile.role = UserProfile.Role.MANAGER
        self.out_of_scope_manager.profile.save(update_fields=["role"])
        self.out_of_scope_manager.profile.allowed_locations.add(self.other_root)
        self.superuser = User.objects.create_superuser(
            username="scan-api-superuser",
            email="scan-api-superuser@example.com",
            password="test-pass-123",
        )
        self.asset = Asset.objects.create(
            name="API Asset",
            inventory_number="API-ASSET-001",
            asset_type=Asset.AssetType.FIXED,
            barcode="BC-API-ASSET",
            location=self.child.path,
            location_fk=self.child,
            status=Asset.Status.IN_STOCK,
        )
        self.session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.root],
            asset_types=[Asset.AssetType.FIXED],
        )
        self.other_session = start_inventory_session(
            created_by=self.user,
            root_locations=[self.other_root],
            asset_types=[Asset.AssetType.FIXED],
        )
        self.url = reverse("inventory:scan-file-import-api")

    def _post_text(self, raw_text, user=None):
        if user is not None:
            self.client.force_login(user)
        return self.client.post(self.url, data=raw_text, content_type="text/plain")

    @override_settings(DEBUG=False)
    def test_anonymous_post_redirects_to_login_when_debug_false(self):
        response = self._post_text(f"{self.session.number}\n{self.child.code}\nBC-API-ASSET")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    @override_settings(DEBUG=True)
    def test_anonymous_post_imports_with_first_superuser_when_debug_true(self):
        response = self._post_text(f"{self.session.number}\n{self.child.code}\nBC-API-ASSET")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        batch = InventoryScanBatch.objects.get()
        self.assertEqual(batch.uploaded_by, self.superuser)

    def test_logged_user_posts_text_plain_and_gets_ok(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.user,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["session"], self.session.number)

    def test_user_can_import_to_session_in_scope(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.user,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_user_cannot_import_to_session_outside_scope(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.out_of_scope_user,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(response.json()["message"], "Brak dostępu do tej sesji inwentaryzacji.")

    def test_forbidden_import_does_not_create_scan_batch(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.out_of_scope_user,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(InventoryScanBatch.objects.count(), 0)

    def test_manager_can_import_to_session_in_scope(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.manager_user,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_manager_cannot_import_to_session_outside_scope(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.out_of_scope_manager,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["message"], "Brak dostępu do tej sesji inwentaryzacji.")
        self.assertEqual(InventoryScanBatch.objects.count(), 0)

    def test_superuser_can_import_to_any_session(self):
        response = self._post_text(
            f"{self.other_session.number}\n{self.other_child.code}\nUNKNOWN-SUPERUSER",
            user=self.superuser,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"], self.other_session.number)

    def test_endpoint_creates_scan_batch(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.user,
        )

        batch = InventoryScanBatch.objects.get()
        self.assertEqual(response.json()["batch_id"], batch.id)
        self.assertEqual(batch.raw_text, f"{self.session.number}\n{self.child.code}\nBC-API-ASSET")
        self.assertEqual(batch.uploaded_by, self.user)

    def test_endpoint_updates_observed_item(self):
        self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET",
            user=self.user,
        )

        observed = InventoryObservedItem.objects.get(asset=self.asset)
        self.assertEqual(observed.status, InventoryObservedItem.Status.FOUND_OK)
        self.assertEqual(observed.scanned_location, self.child)

    def test_empty_body_returns_400(self):
        response = self._post_text("", user=self.user)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(InventoryScanBatch.objects.count(), 0)

    def test_missing_session_returns_400(self):
        response = self._post_text("INV-999999\nBC-API-ASSET", user=self.user)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(InventoryScanBatch.objects.count(), 0)

    def test_closed_session_returns_400(self):
        self.session.status = InventorySession.Status.CLOSED
        self.session.closed_at = timezone.now()
        self.session.save(update_fields=["status", "closed_at", "updated_at"])

        response = self._post_text(f"{self.session.number}\nBC-API-ASSET", user=self.user)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        self.assertEqual(InventoryScanBatch.objects.count(), 0)

    def test_success_json_contains_batch_id_and_counters(self):
        response = self._post_text(
            f"{self.session.number}\n{self.child.code}\nBC-API-ASSET\nUNKNOWN-API",
            user=self.user,
        )

        payload = response.json()
        self.assertIn("batch_id", payload)
        self.assertEqual(payload["total_lines"], 4)
        self.assertEqual(payload["processed_lines"], 3)
        self.assertEqual(payload["recognized_assets_count"], 1)
        self.assertEqual(payload["unknown_codes_count"], 1)

    def test_get_returns_method_not_allowed(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)

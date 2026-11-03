from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from users.models import User

from .models import Location, OrganizationSettings


class OrganizationSettingsModelTests(TestCase):
    def test_get_creates_default_if_not_exists(self):
        self.assertEqual(OrganizationSettings.objects.count(), 0)
        org = OrganizationSettings.get()
        self.assertEqual(OrganizationSettings.objects.count(), 1)
        self.assertEqual(org.pk, 1)

    def test_get_returns_existing_record(self):
        OrganizationSettings.objects.create(pk=1, full_name="Firma X", short_name="FX")
        org = OrganizationSettings.get()
        self.assertEqual(org.full_name, "Firma X")
        self.assertEqual(OrganizationSettings.objects.count(), 1)

    def test_singleton_enforced_on_save(self):
        OrganizationSettings.objects.create(pk=1, full_name="Stara", short_name="S")
        new = OrganizationSettings(full_name="Nowa", short_name="N")
        new.save()
        self.assertEqual(OrganizationSettings.objects.count(), 1)
        self.assertEqual(OrganizationSettings.objects.get().full_name, "Nowa")

    def test_str_returns_short_name(self):
        org = OrganizationSettings(short_name="GMINA")
        self.assertEqual(str(org), "GMINA")

    def test_str_returns_fallback_when_short_name_empty(self):
        org = OrganizationSettings(short_name="")
        self.assertEqual(str(org), "Organizacja")


class OrganizationSettingsViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="org-test-user", password="test-pass-123")
        self.url = reverse("locations:organization-settings")

    def test_view_requires_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/", response["Location"])

    def test_view_renders(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dane organizacji")
        self.assertContains(response, "full_name")

    def test_view_saves_data(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {
            "full_name": "Urząd Gminy Przykładowo",
            "short_name": "UGP",
            "report_footer": "ul. Testowa 1",
        })
        self.assertEqual(response.status_code, 302)
        org = OrganizationSettings.objects.get(pk=1)
        self.assertEqual(org.full_name, "Urząd Gminy Przykładowo")
        self.assertEqual(org.short_name, "UGP")
        self.assertEqual(org.report_footer, "ul. Testowa 1")


class LocationOptionsApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Location.objects.create(name="Warszawa")
        cls.child = Location.objects.create(name="Magazyn A", parent=cls.root)
        cls.leaf = Location.objects.create(name="Strefa 2", parent=cls.child)
        cls.other_root = Location.objects.create(name="Krakow")
        cls.other_child = Location.objects.create(name="Biuro", parent=cls.other_root)
        cls.inactive = Location.objects.create(name="Archiwum", parent=cls.root, is_active=False)

        cls.admin_user = User.objects.create_superuser(
            username="location-admin",
            email="location-admin@example.com",
            password="test-pass-123",
        )
        cls.scoped_user = User.objects.create_user(username="location-user", password="test-pass-123")
        cls.scoped_user.profile.role = UserProfile.Role.USER
        cls.scoped_user.profile.save(update_fields=["role"])
        cls.scoped_user.profile.allowed_locations.add(cls.root)

        cls.no_access_user = User.objects.create_user(username="location-empty", password="test-pass-123")
        cls.no_access_user.profile.role = UserProfile.Role.USER
        cls.no_access_user.profile.save(update_fields=["role"])

    def test_admin_sees_all_active_locations(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertIn("locations", payload)
        self.assertTrue(payload["locations"])
        self.assertTrue(all("id" in item and "path" in item for item in payload["locations"]))
        self.assertIn(
            {"id": self.leaf.id, "path": "Warszawa / Magazyn A / Strefa 2"},
            payload["locations"],
        )
        self.assertIn(
            {"id": self.other_child.id, "path": "Krakow / Biuro"},
            payload["locations"],
        )
        self.assertNotIn(
            {"id": self.inactive.id, "path": "Warszawa / Archiwum"},
            payload["locations"],
        )

    def test_user_sees_allowed_location(self):
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn({"id": self.root.id, "path": "Warszawa"}, payload["locations"])

    def test_user_sees_children_of_allowed_location(self):
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn({"id": self.child.id, "path": "Warszawa / Magazyn A"}, payload["locations"])
        self.assertIn({"id": self.leaf.id, "path": "Warszawa / Magazyn A / Strefa 2"}, payload["locations"])

    def test_user_does_not_see_locations_outside_scope(self):
        self.client.force_login(self.scoped_user)

        response = self.client.get(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn({"id": self.other_root.id, "path": "Krakow"}, payload["locations"])
        self.assertNotIn({"id": self.other_child.id, "path": "Krakow / Biuro"}, payload["locations"])

    def test_user_without_allowed_locations_gets_empty_list(self):
        self.client.force_login(self.no_access_user)

        response = self.client.get(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["locations"], [])

    def test_api_rejects_non_get(self):
        response = self.client.post(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["success"], False)


class LocationPrintingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from inventory.models import InventorySession
        cls.root = Location.objects.create(name="Budynek A")
        cls.room = Location.objects.create(name="Pokój 101", parent=cls.root)
        cls.empty_room = Location.objects.create(name="Pusty pokój", parent=cls.root)
        cls.inactive = Location.objects.create(name="Nieczynny", parent=cls.root, is_active=False)
        cls.other = Location.objects.create(name="Budynek B")
        cls.admin = User.objects.create_superuser(username="print-admin", password="pass")
        cls.worker = User.objects.create_user(username="print-worker", password="pass")
        profile, _ = UserProfile.objects.get_or_create(user=cls.worker)
        profile.allowed_locations.set([cls.room])
        cls.session = InventorySession.objects.create(number="INV-PRINT", created_by=cls.admin)
        cls.session.scope_root_locations.set([cls.root])

    def test_print_endpoints_require_login(self):
        for url in [reverse("locations:labels-pdf"), reverse("inventory:session-location-codes", args=[self.session.pk])]:
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_labels_validate_ids_and_access(self):
        self.client.force_login(self.worker)
        url = reverse("locations:labels-pdf")
        for params in [{}, {"ids": "bad"}, {"ids": "-1"}]:
            self.assertEqual(self.client.get(url, params).status_code, 400)
        for ids in [[self.other.pk], [self.room.pk, self.other.pk], [self.inactive.pk], [999999]]:
            self.assertEqual(self.client.get(url, {"ids": ids}).status_code, 404)
        response = self.client.get(url, {"ids": [self.room.pk, self.room.pk]})
        self.assertEqual(response.status_code, 200)
        raw = b"".join(response.streaming_content)
        self.assertTrue(raw.startswith(b"%PDF-"))
        self.assertEqual(raw.count(b"/Type /Page") - raw.count(b"/Type /Pages"), 1)

    def test_sheet_contains_descendants_and_empty_rooms_only_in_scope(self):
        from unittest.mock import patch
        from io import BytesIO
        self.client.force_login(self.admin)
        with patch("locations.printing.generate_location_sheet_pdf", return_value=BytesIO(b"pdf")) as generator:
            response = self.client.get(reverse("inventory:session-location-codes", args=[self.session.pk]))
            self.assertEqual(response.status_code, 200)
            locations, organization, number = generator.call_args.args
            self.assertSetEqual({loc.pk for loc in locations}, {self.root.pk, self.room.pk, self.empty_room.pk})
            self.assertEqual(number, self.session.number)

    def test_sheet_respects_location_permissions_and_session_visibility(self):
        from unittest.mock import patch
        from io import BytesIO
        from inventory.models import InventorySession
        self.client.force_login(self.worker)
        with patch("locations.printing.generate_location_sheet_pdf", return_value=BytesIO(b"pdf")) as generator:
            response = self.client.get(reverse("inventory:session-location-codes", args=[self.session.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual([loc.pk for loc in generator.call_args.args[0]], [self.room.pk])
        hidden = InventorySession.objects.create(number="INV-HIDDEN", created_by=self.admin)
        hidden.scope_root_locations.set([self.other])
        self.assertEqual(self.client.get(reverse("inventory:session-location-codes", args=[hidden.pk])).status_code, 404)

    def test_sheet_is_available_in_lite_and_does_not_create_organization(self):
        self.client.force_login(self.admin)
        before = OrganizationSettings.objects.count()
        with self.settings(LUPINE_EDITION="lite"):
            response = self.client.get(reverse("inventory:session-location-codes", args=[self.session.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
        self.assertEqual(OrganizationSettings.objects.count(), before)

    def test_a4_pagination_and_label_page_count(self):
        from .printing import generate_location_sheet_pdf, generate_location_labels_pdf
        locations = [self.room] * 13
        for generator, args, pages in [
            (generate_location_sheet_pdf, (locations, "Żyrardów", "INV-PRINT"), 2),
            (generate_location_labels_pdf, (locations, "Żyrardów"), 13),
        ]:
            raw = generator(*args).getvalue()
            self.assertEqual(raw.count(b"/Type /Page") - raw.count(b"/Type /Pages"), pages)

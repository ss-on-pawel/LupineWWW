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

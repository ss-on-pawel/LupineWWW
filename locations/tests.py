from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from users.models import User

from .models import Location


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

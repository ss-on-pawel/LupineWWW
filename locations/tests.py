from django.test import TestCase
from django.urls import reverse

from .models import Location


class LocationOptionsApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Location.objects.create(name="Warszawa")
        cls.child = Location.objects.create(name="Magazyn A", parent=cls.root)
        cls.leaf = Location.objects.create(name="Strefa 2", parent=cls.child)

    def test_api_returns_locations_with_full_paths(self):
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

    def test_api_rejects_non_get(self):
        response = self.client.post(reverse("locations:api-options"))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["success"], False)

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from locations.models import Location


class SeedLocationsCommandTests(TestCase):
    def test_seed_locations_creates_requested_hierarchy(self):
        stdout = StringIO()

        call_command("seed_locations", count=40, cities=3, stdout=stdout)

        self.assertEqual(Location.objects.count(), 40)
        self.assertEqual(Location.objects.filter(parent__isnull=True).count(), 3)
        self.assertTrue(Location.objects.filter(parent__parent__isnull=False).exists())
        self.assertTrue(Location.objects.exclude(code="").filter(code__startswith="LOC-").exists())
        self.assertIn("Liczba wszystkich lokalizacji: 40", stdout.getvalue())

    def test_seed_locations_requires_empty_table_without_clear(self):
        Location.objects.create(name="Warszawa")

        with self.assertRaises(CommandError):
            call_command("seed_locations", count=10, cities=1)

    def test_seed_locations_supports_many_city_roots(self):
        call_command("seed_locations", count=200, cities=50)

        self.assertEqual(Location.objects.count(), 200)
        self.assertEqual(Location.objects.filter(parent__isnull=True).count(), 50)
        self.assertTrue(Location.objects.filter(name="Miasto 001", parent__isnull=True).exists())
        self.assertTrue(Location.objects.filter(name="Miasto 050", parent__isnull=True).exists())

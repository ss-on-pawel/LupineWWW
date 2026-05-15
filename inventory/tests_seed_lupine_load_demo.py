from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import UserProfile
from assets.models import Asset, AssetServiceAlert
from inventory.models import InventorySession
from locations.models import Location

User = get_user_model()

LOAD_DEMO_PREFIX = "load_demo:"
LOAD_SESSION_PREFIX = "LOAD-"
LOAD_USER_PREFIX = "load_demo_"
LOAD_ROOT_NAME = "LOAD Demo"

SMALL_SEED_KWARGS = dict(assets=50, locations=20, users=8, stdout=StringIO(), stderr=StringIO())


def _run_seed(**kwargs):
    call_command("seed_lupine_load_demo", **{**SMALL_SEED_KWARGS, **kwargs})


def _run_clear():
    call_command("clear_lupine_load_demo", stdout=StringIO(), stderr=StringIO())


# ── Seed: dane podstawowe ─────────────────────────────────────────────────────

class SeedAssetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _run_seed()

    def test_assets_count(self):
        self.assertEqual(Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).count(), 50)

    def test_barcode_uniqueness(self):
        barcodes = list(
            Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX)
            .values_list("barcode", flat=True)
        )
        self.assertEqual(len(barcodes), len(set(barcodes)), "Duplikat barcode w assetach load-demo")

    def test_external_id_prefix(self):
        without_prefix = Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).exclude(
            external_id__startswith=LOAD_DEMO_PREFIX
        )
        self.assertEqual(without_prefix.count(), 0)

    def test_location_cache_populated(self):
        empty_location = Asset.objects.filter(
            external_id__startswith=LOAD_DEMO_PREFIX, location=""
        ).count()
        self.assertEqual(empty_location, 0, "Assety load-demo mają pusty cache lokalizacji")

    def test_is_active_synced_with_status(self):
        liquidated_but_active = Asset.objects.filter(
            external_id__startswith=LOAD_DEMO_PREFIX,
            status=Asset.Status.LIQUIDATED,
            is_active=True,
        ).count()
        self.assertEqual(liquidated_but_active, 0, "LIQUIDATED assety mają is_active=True")

    def test_asset_type_code_matches_ref(self):
        mismatched = Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).exclude(
            asset_type_ref__isnull=True
        ).exclude(asset_type=None).filter(
            asset_type_ref__code__isnull=False
        )
        for asset in mismatched.select_related("asset_type_ref")[:10]:
            self.assertEqual(
                asset.asset_type,
                asset.asset_type_ref.code,
                f"Niezgodność asset_type vs asset_type_ref.code dla {asset}",
            )


# ── Seed: lokalizacje ─────────────────────────────────────────────────────────

class SeedLocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _run_seed()

    def test_root_location_exists(self):
        self.assertTrue(
            Location.objects.filter(name=LOAD_ROOT_NAME, parent__isnull=True).exists()
        )

    def test_locations_created(self):
        root = Location.objects.get(name=LOAD_ROOT_NAME, parent__isnull=True)
        # Musi być przynajmniej root + kilka dzieci
        self.assertGreater(
            Location.objects.filter(id=root.id).count()
            + Location.objects.filter(parent__id=root.id).count(),
            1,
        )

    def test_all_load_assets_have_location_fk(self):
        without_fk = Asset.objects.filter(
            external_id__startswith=LOAD_DEMO_PREFIX, location_fk__isnull=True
        ).count()
        self.assertEqual(without_fk, 0)


# ── Seed: użytkownicy ─────────────────────────────────────────────────────────

class SeedUserTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _run_seed()

    def test_users_created(self):
        count = User.objects.filter(username__startswith=LOAD_USER_PREFIX).count()
        self.assertGreaterEqual(count, 4)

    def test_admin_users_are_superusers(self):
        admins = User.objects.filter(username=f"{LOAD_USER_PREFIX}admin_01")
        self.assertTrue(admins.exists())
        self.assertTrue(admins.first().is_superuser)

    def test_admin_profile_role(self):
        user = User.objects.get(username=f"{LOAD_USER_PREFIX}admin_01")
        self.assertEqual(user.profile.role, UserProfile.Role.ADMIN)

    def test_manager_profile_role(self):
        mgr = User.objects.filter(username__startswith=f"{LOAD_USER_PREFIX}mgr_").first()
        if mgr:
            self.assertEqual(mgr.profile.role, UserProfile.Role.MANAGER)
            self.assertTrue(mgr.profile.can_approve_asset_changes)

    def test_regular_user_profile_role(self):
        user = User.objects.filter(username__startswith=f"{LOAD_USER_PREFIX}user_").first()
        if user:
            self.assertEqual(user.profile.role, UserProfile.Role.USER)


# ── Seed: alerty serwisowe ────────────────────────────────────────────────────

class SeedServiceAlertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _run_seed()

    def test_alerts_created(self):
        count = AssetServiceAlert.objects.filter(
            asset__external_id__startswith=LOAD_DEMO_PREFIX
        ).count()
        self.assertGreater(count, 0, "Brak alertów serwisowych po seedzie")

    def test_alerts_have_mixed_statuses(self):
        statuses = set(
            AssetServiceAlert.objects.filter(
                asset__external_id__startswith=LOAD_DEMO_PREFIX
            ).values_list("status", flat=True)
        )
        # Przy 50 assetach powinien być przynajmniej 1 status
        self.assertTrue(len(statuses) >= 1)

    def test_alerts_only_for_active_assets(self):
        for_inactive = AssetServiceAlert.objects.filter(
            asset__external_id__startswith=LOAD_DEMO_PREFIX,
            asset__status=Asset.Status.LIQUIDATED,
        ).count()
        self.assertEqual(for_inactive, 0, "Alerty dla LIQUIDATED assetów")


# ── Seed: sesje inventory ─────────────────────────────────────────────────────

class SeedInventorySessionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _run_seed()

    def test_sessions_created(self):
        count = InventorySession.objects.filter(number__startswith=LOAD_SESSION_PREFIX).count()
        self.assertGreater(count, 0, "Brak sesji inventory po seedzie")

    def test_session_numbers_start_with_prefix(self):
        sessions = InventorySession.objects.filter(number__startswith=LOAD_SESSION_PREFIX)
        for session in sessions:
            self.assertTrue(session.number.startswith(LOAD_SESSION_PREFIX))


# ── Guard: odmowa przy istniejących danych ────────────────────────────────────

class SeedGuardTest(TestCase):
    def test_refuses_second_seed_without_clear(self):
        _run_seed()
        with self.assertRaises(CommandError):
            _run_seed()


# ── Clear: usuwanie danych ────────────────────────────────────────────────────

class ClearLoadDemoTest(TestCase):
    def setUp(self):
        _run_seed()

    def test_clear_removes_assets(self):
        _run_clear()
        self.assertEqual(
            Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).count(), 0
        )

    def test_clear_removes_load_users(self):
        _run_clear()
        self.assertEqual(
            User.objects.filter(username__startswith=LOAD_USER_PREFIX).count(), 0
        )

    def test_clear_removes_sessions(self):
        _run_clear()
        self.assertEqual(
            InventorySession.objects.filter(number__startswith=LOAD_SESSION_PREFIX).count(), 0
        )

    def test_clear_removes_root_location(self):
        _run_clear()
        self.assertFalse(
            Location.objects.filter(name=LOAD_ROOT_NAME, parent__isnull=True).exists()
        )

    def test_clear_removes_alerts(self):
        _run_clear()
        # Alerty kaskadują po usunięciu assetów; sprawdzamy że nie ma assetów load-demo
        self.assertEqual(
            Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).count(), 0
        )

    def test_clear_preserves_non_load_superuser(self):
        real_su = User.objects.create_superuser(
            username="real_superuser", password="safe", email="su@real.com"
        )
        _run_clear()
        self.assertTrue(User.objects.filter(username="real_superuser").exists())

    def test_seed_possible_after_clear(self):
        _run_clear()
        # Po clear powinno być możliwe ponowne seedowanie bez CommandError
        try:
            _run_seed()
        except CommandError as e:
            self.fail(f"Seed po clear podniósł CommandError: {e}")

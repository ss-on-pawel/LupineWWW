from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import UserProfile
from accounts.views import _is_panel_admin

User = get_user_model()


class SeedLupineDemo5000UsersTests(TestCase):
    """Weryfikuje flagi demo userów po uruchomieniu seed_lupine_demo_5000."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_lupine_demo_5000", asset_count=100, stdout=StringIO())
        cls.manager = User.objects.get(username="demo.manager")
        cls.admin = User.objects.get(username="demo.admin")
        cls.user = User.objects.get(username="demo.user")

    # ── demo.manager ────────────────────────────────────────────────────────

    def test_demo_manager_is_not_staff(self):
        self.assertFalse(self.manager.is_staff)

    def test_demo_manager_is_not_superuser(self):
        self.assertFalse(self.manager.is_superuser)

    def test_demo_manager_has_manager_role(self):
        self.assertEqual(self.manager.profile.role, UserProfile.Role.MANAGER)

    def test_demo_manager_can_approve_asset_changes(self):
        self.assertTrue(self.manager.profile.can_approve_asset_changes)

    def test_demo_manager_denied_user_panel(self):
        self.assertFalse(_is_panel_admin(self.manager))

    # ── demo.admin ───────────────────────────────────────────────────────────

    def test_demo_admin_is_superuser(self):
        self.assertTrue(self.admin.is_superuser)

    def test_demo_admin_has_admin_role(self):
        self.assertEqual(self.admin.profile.role, UserProfile.Role.ADMIN)

    def test_demo_admin_allowed_user_panel(self):
        self.assertTrue(_is_panel_admin(self.admin))

    # ── demo.user ────────────────────────────────────────────────────────────

    def test_demo_user_is_not_staff(self):
        self.assertFalse(self.user.is_staff)

    def test_demo_user_is_not_superuser(self):
        self.assertFalse(self.user.is_superuser)

    def test_demo_user_has_user_role(self):
        self.assertEqual(self.user.profile.role, UserProfile.Role.USER)

    def test_demo_user_cannot_approve_asset_changes(self):
        self.assertFalse(self.user.profile.can_approve_asset_changes)

    def test_demo_user_denied_user_panel(self):
        self.assertFalse(_is_panel_admin(self.user))

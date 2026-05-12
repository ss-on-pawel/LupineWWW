from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from users.models import User

from .models import UserProfile


class UserProfileSignalTests(TestCase):
    def test_profile_is_created_for_new_user(self):
        user = User.objects.create_user(username="alice", password="test-pass-123")

        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.role, UserProfile.Role.USER)
        self.assertFalse(user.profile.can_approve_asset_changes)
        self.assertFalse(user.profile.asset_changes_require_approval)


class LoginViewTests(TestCase):
    def test_login_page_is_available(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logowanie")


class UserListAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staffuser", password="pass123", is_staff=True
        )
        self.superuser = User.objects.create_superuser(
            username="superuser", password="pass123"
        )
        self.admin_role = User.objects.create_user(
            username="adminrole", password="pass123"
        )
        self.admin_role.profile.role = UserProfile.Role.ADMIN
        self.admin_role.profile.save()

        self.regular = User.objects.create_user(
            username="regular", password="pass123"
        )

    def test_user_list_redirects_anonymous(self):
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_user_list_accessible_by_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 200)

    def test_user_list_accessible_by_superuser(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 200)

    def test_user_list_accessible_by_admin_role(self):
        self.client.force_login(self.admin_role)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 200)

    def test_user_list_forbidden_for_regular_user(self):
        self.client.force_login(self.regular)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 403)


class UserCreateTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staffuser", password="pass123", is_staff=True
        )
        self.client.force_login(self.staff)

    def test_create_form_renders(self):
        response = self.client.get(reverse("accounts:user-create"))
        self.assertEqual(response.status_code, 200)

    def test_create_user_basic(self):
        response = self.client.post(
            reverse("accounts:user-create"),
            {
                "username": "newuser",
                "password": "strongpass99",
                "first_name": "Jan",
                "last_name": "Kowalski",
                "email": "jan@example.com",
                "role": "user",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        self.assertRedirects(response, reverse("accounts:user-list"))
        self.assertTrue(User.objects.filter(username="newuser").exists())

    def test_manager_role_sets_can_approve(self):
        self.client.post(
            reverse("accounts:user-create"),
            {
                "username": "mgruser",
                "password": "strongpass99",
                "first_name": "",
                "last_name": "",
                "email": "",
                "role": "manager",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        user = User.objects.get(username="mgruser")
        self.assertEqual(user.profile.role, UserProfile.Role.MANAGER)
        self.assertTrue(user.profile.can_approve_asset_changes)

    def test_user_role_clears_can_approve(self):
        self.client.post(
            reverse("accounts:user-create"),
            {
                "username": "basicuser",
                "password": "strongpass99",
                "first_name": "",
                "last_name": "",
                "email": "",
                "role": "user",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        user = User.objects.get(username="basicuser")
        self.assertEqual(user.profile.role, UserProfile.Role.USER)
        self.assertFalse(user.profile.can_approve_asset_changes)

    def test_admin_role_sets_can_approve(self):
        self.client.post(
            reverse("accounts:user-create"),
            {
                "username": "adminuser",
                "password": "strongpass99",
                "first_name": "",
                "last_name": "",
                "email": "",
                "role": "admin",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        user = User.objects.get(username="adminuser")
        self.assertEqual(user.profile.role, UserProfile.Role.ADMIN)
        self.assertTrue(user.profile.can_approve_asset_changes)


class UserEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staffuser", password="pass123", is_staff=True
        )
        self.target = User.objects.create_user(
            username="targetuser",
            password="pass123",
            first_name="Anna",
            last_name="Nowak",
        )
        self.client.force_login(self.staff)

    def test_edit_form_renders(self):
        response = self.client.get(
            reverse("accounts:user-edit", args=[self.target.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_edit_user_changes_role(self):
        self.client.post(
            reverse("accounts:user-edit", args=[self.target.pk]),
            {
                "username": "targetuser",
                "first_name": "Anna",
                "last_name": "Nowak",
                "email": "",
                "role": "manager",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        self.target.profile.refresh_from_db()
        self.assertEqual(self.target.profile.role, UserProfile.Role.MANAGER)
        self.assertTrue(self.target.profile.can_approve_asset_changes)

    def test_edit_downgrade_role_clears_approve(self):
        self.target.profile.role = UserProfile.Role.MANAGER
        self.target.profile.can_approve_asset_changes = True
        self.target.profile.save()

        self.client.post(
            reverse("accounts:user-edit", args=[self.target.pk]),
            {
                "username": "targetuser",
                "first_name": "Anna",
                "last_name": "Nowak",
                "email": "",
                "role": "user",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        self.target.profile.refresh_from_db()
        self.assertEqual(self.target.profile.role, UserProfile.Role.USER)
        self.assertFalse(self.target.profile.can_approve_asset_changes)

    def test_edit_requires_login(self):
        self.client.logout()
        response = self.client.get(
            reverse("accounts:user-edit", args=[self.target.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_edit_forbidden_for_regular_user(self):
        regular = User.objects.create_user(username="reguser", password="pass123")
        self.client.force_login(regular)
        response = self.client.get(
            reverse("accounts:user-edit", args=[self.target.pk])
        )
        self.assertEqual(response.status_code, 403)


class UserPasswordResetTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="resetstaff", password="pass123", is_staff=True
        )
        self.target = User.objects.create_user(
            username="resetarget",
            password="oldpassword",
            email="target@example.com",
        )
        self.client.force_login(self.staff)

    def _reset_url(self):
        return reverse("accounts:user-reset-password", args=[self.target.pk])

    def test_reset_changes_password_hash(self):
        old_hash = self.target.password
        self.client.post(self._reset_url())
        self.target.refresh_from_db()
        self.assertNotEqual(self.target.password, old_hash)

    def test_reset_stores_hashed_password_not_plaintext(self):
        self.client.post(self._reset_url())
        self.target.refresh_from_db()
        self.assertTrue(
            self.target.password.startswith(("pbkdf2", "bcrypt", "argon2", "scrypt")),
            "Hasło powinno być zahashowane, nie plaintext.",
        )

    def test_reset_generated_password_is_usable(self):
        with patch("accounts.views.generate_temp_password", return_value="LUP-TEST-1234"):
            self.client.post(self._reset_url())
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("LUP-TEST-1234"))

    def test_reset_sends_email_when_user_has_email(self):
        self.client.post(self._reset_url())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["target@example.com"])
        self.assertIn("resetarget", mail.outbox[0].body)
        self.assertIn("LUP-", mail.outbox[0].body)

    def test_reset_email_subject(self):
        self.client.post(self._reset_url())
        self.assertEqual(mail.outbox[0].subject, "LupineWWW — nowe hasło")

    def test_reset_fallback_no_email_shows_password_in_message(self):
        no_email_user = User.objects.create_user(
            username="noemailuser", password="oldpass"
        )
        url = reverse("accounts:user-reset-password", args=[no_email_user.pk])
        response = self.client.post(url, follow=True)
        messages_text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("LUP-", messages_text)
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_fallback_no_email_does_not_send_mail(self):
        no_email_user = User.objects.create_user(
            username="noemailuser2", password="oldpass"
        )
        url = reverse("accounts:user-reset-password", args=[no_email_user.pk])
        self.client.post(url)
        self.assertEqual(len(mail.outbox), 0)

    def test_reset_forbidden_for_regular_user(self):
        regular = User.objects.create_user(username="resetregular", password="pass123")
        self.client.force_login(regular)
        response = self.client.post(self._reset_url())
        self.assertEqual(response.status_code, 403)

    def test_reset_redirects_anonymous_to_login(self):
        self.client.logout()
        response = self.client.post(self._reset_url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_reset_rejects_get_request(self):
        response = self.client.get(self._reset_url())
        self.assertEqual(response.status_code, 405)

    def test_generate_temp_password_format(self):
        from accounts.views import generate_temp_password
        pwd = generate_temp_password()
        parts = pwd.split("-")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "LUP")
        self.assertEqual(len(parts[1]), 4)
        self.assertEqual(len(parts[2]), 4)


class ManagerPanelAccessRegressionTests(TestCase):
    """Manager role must never access the user management panel."""

    def setUp(self):
        self.manager = User.objects.create_user(
            username="regressionmgr", password="pass123"
        )
        self.manager.profile.role = UserProfile.Role.MANAGER
        self.manager.profile.can_approve_asset_changes = True
        self.manager.profile.save()
        self.client.force_login(self.manager)

        self.other = User.objects.create_user(username="regressionother", password="pass123")

    def test_manager_cannot_access_user_list(self):
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_get_user_create(self):
        response = self.client.get(reverse("accounts:user-create"))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_post_user_create(self):
        response = self.client.post(
            reverse("accounts:user-create"),
            {
                "username": "newbymanager",
                "password": "somepass99",
                "role": "user",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(User.objects.filter(username="newbymanager").exists())

    def test_manager_cannot_get_user_edit(self):
        response = self.client.get(reverse("accounts:user-edit", args=[self.other.pk]))
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_post_user_edit(self):
        response = self.client.post(
            reverse("accounts:user-edit", args=[self.other.pk]),
            {
                "username": "regressionother",
                "role": "admin",
                "is_active": True,
                "allowed_locations": [],
            },
        )
        self.assertEqual(response.status_code, 403)
        self.other.profile.refresh_from_db()
        self.assertNotEqual(self.other.profile.role, UserProfile.Role.ADMIN)

    def test_manager_cannot_reset_password(self):
        response = self.client.post(
            reverse("accounts:user-reset-password", args=[self.other.pk])
        )
        self.assertEqual(response.status_code, 403)

    def test_panel_still_accessible_by_superuser(self):
        su = User.objects.create_superuser(username="regression_su", password="pass123")
        self.client.force_login(su)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 200)

    def test_panel_still_accessible_by_staff(self):
        staff = User.objects.create_user(
            username="regression_staff", password="pass123", is_staff=True
        )
        self.client.force_login(staff)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 200)

    def test_panel_still_accessible_by_admin_role(self):
        admin = User.objects.create_user(username="regression_adminrole", password="pass123")
        admin.profile.role = UserProfile.Role.ADMIN
        admin.profile.save()
        self.client.force_login(admin)
        response = self.client.get(reverse("accounts:user-list"))
        self.assertEqual(response.status_code, 200)

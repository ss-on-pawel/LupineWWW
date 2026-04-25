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


class LoginViewTests(TestCase):
    def test_login_page_is_available(self):
        response = self.client.get(reverse("accounts:login"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logowanie")

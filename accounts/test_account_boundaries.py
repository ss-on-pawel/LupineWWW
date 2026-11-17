from django.test import TestCase
from django.urls import reverse
from users.models import User


class AccountBoundaryTests(TestCase):
    def test_application_admin_cannot_edit_or_reset_privileged_accounts(self):
        actor = User.objects.create_user(username='app-admin')
        actor.profile.role = 'admin'
        actor.profile.save()
        self.client.force_login(actor)
        for staff, superuser in [(True, False), (True, True)]:
            target = User.objects.create_user(username=f'target-{superuser}', is_staff=staff,
                                              is_superuser=superuser, email='original@example.com')
            before = (target.password, target.email, target.is_active)
            for operation in ['user-edit', 'user-reset-password']:
                url = reverse(f'accounts:{operation}', args=[target.pk])
                if operation == 'user-edit':
                    self.assertEqual(self.client.get(url).status_code, 403)
                response = self.client.post(url, {'username': target.username, 'email': 'changed@example.com',
                                                  'is_active': False, 'role': 'user'})
                self.assertEqual(response.status_code, 403)
            target.refresh_from_db()
            self.assertEqual((target.password, target.email, target.is_active), before)
            response = self.client.get(reverse('accounts:user-list'))
            self.assertNotContains(response, reverse('accounts:user-edit', args=[target.pk]))
            self.assertNotContains(response, reverse('accounts:user-reset-password', args=[target.pk]))

    def test_staff_cannot_reset_superuser_but_superuser_can_reset_staff(self):
        staff = User.objects.create_user(username='staff', is_staff=True)
        superuser = User.objects.create_superuser(username='root')
        self.client.force_login(staff)
        self.assertEqual(self.client.post(reverse('accounts:user-reset-password', args=[superuser.pk])).status_code, 403)
        before = staff.password
        self.client.force_login(superuser)
        self.assertEqual(self.client.post(reverse('accounts:user-reset-password', args=[staff.pk])).status_code, 302)
        staff.refresh_from_db()
        self.assertNotEqual(staff.password, before)

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.forms.models import model_to_dict
from django.test import Client, TestCase
from django.urls import reverse

from users.models import User
from locations.models import Location
from .models import Asset, AssetCollection, AssetCollectionEvent, AssetDepreciationPlan
from .collections import create_collection
from .collection_views import collection_context
from .forms import AssetForm
from .withdrawals import withdraw_asset


class CollectionInterfaceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username='group-ui-admin', password='test')
        self.user = User.objects.create_user(username='group-ui-user', password='test')
        self.room = Location.objects.create(name='UI Office', code='GUI-ROOM')
        self.other_room = Location.objects.create(name='Secret Office', code='GUI-OTHER')
        self.user.profile.allowed_locations.add(self.room)
        self.assets = [Asset.objects.create(name=f'UI Item {i}', inventory_number=f'UI-INV-{i}',
            barcode=f'GUI-{i}', asset_type='fixed', location_fk=self.room) for i in range(4)]
        self.secret = Asset.objects.create(name='Confidential', barcode='GUI-SECRET', location_fk=self.other_room)
        self.client.force_login(self.user)

    def group(self):
        return create_collection(operator=self.admin, kind='group', name='UI Group',
            asset_ids=[a.pk for a in self.assets[:2]], request_id=uuid.uuid4())

    def strict_set(self):
        return create_collection(operator=self.admin, kind='set', name='Accounting set',
            parent_id=self.assets[0].pk, asset_ids=[a.pk for a in self.assets[:3]], request_id=uuid.uuid4())

    def change(self, group, action='attach', ids=None, **kwargs):
        data = dict(action=action, request_id=str(uuid.uuid4()), expected_version=group.version,
            asset_ids=ids or [self.assets[2].pk])
        data.update(kwargs)
        return self.client.post(reverse('assets:collection-detail', args=[group.pk]), data)

    def test_card_uses_same_collapsible_section_and_group_create_link(self):
        response = self.client.get(reverse('assets:detail', args=[self.assets[0].pk]))
        self.assertContains(response, 'aria-controls="sekcja-zestaw"')
        self.assertContains(response, 'id="sekcja-zestaw"')
        self.assertContains(response, reverse('assets:group-create', args=[self.assets[0].pk]))

    def test_group_creation_and_replay_leave_fields_unchanged(self):
        url = reverse('assets:group-create', args=[self.assets[0].pk])
        data = dict(name='Workstation', request_id=str(uuid.uuid4()), asset_ids=[self.assets[1].pk])
        before = model_to_dict(self.assets[1])
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(AssetCollection.objects.count(), 1)
        self.assertEqual(AssetCollectionEvent.objects.count(), 1)
        self.assets[1].refresh_from_db()
        self.assertEqual(before, model_to_dict(self.assets[1]))
        self.assertContains(self.client.get(response.url), 'Workstation')

    def test_group_members_can_attach_detach_and_retry(self):
        group = self.group()
        key = str(uuid.uuid4())
        self.assertEqual(self.change(group, request_id=key).status_code, 302)
        self.assertEqual(self.change(group, request_id=key).status_code, 302)
        group.refresh_from_db()
        self.assertEqual(group.memberships.filter(left_at__isnull=True).count(), 3)
        self.assertEqual(self.change(group, 'detach', [self.assets[1].pk]).status_code, 302)
        self.assertTrue(group.memberships.filter(asset=self.assets[1], left_at__isnull=False).exists())

    def test_invalid_stale_or_empty_post_shows_error_without_writes(self):
        group = self.group()
        self.assertEqual(self.change(group).status_code, 302)
        stale = self.change(group, 'detach', [self.assets[1].pk])
        self.assertContains(stale, 'Skład zmienił się')
        invalid = self.change(group, action='garbage')
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(AssetCollectionEvent.objects.count(), 2)

    def test_create_and_change_respect_direct_ids_and_entire_collection_scope(self):
        url = reverse('assets:group-create', args=[self.assets[0].pk])
        response = self.client.post(url, dict(name='Bad', request_id=str(uuid.uuid4()), asset_ids=[self.secret.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AssetCollection.objects.count(), 0)
        group = self.group()
        Asset.objects.filter(pk=self.assets[1].pk).update(location_fk=self.other_room)
        detail = self.client.get(reverse('assets:collection-detail', args=[group.pk]))
        self.assertNotContains(detail, self.assets[1].barcode)
        self.assertEqual(self.change(group).status_code, 403)

    def test_private_collection_hidden_and_anonymous_redirected(self):
        group = create_collection(operator=self.admin, kind='group', name='Secret Group',
            asset_ids=[self.secret.pk], request_id=uuid.uuid4())
        url = reverse('assets:collection-detail', args=[group.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_csrf_is_required(self):
        group = self.group()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(reverse('assets:collection-detail', args=[group.pk]), {}).status_code, 403)

    def test_group_available_in_all_lite_modes_without_enabling_depreciation(self):
        group = self.group()
        url = reverse('assets:collection-detail', args=[group.pk])
        with self.settings(LUPINE_EDITION='lite'):
            self.assertContains(self.client.get(url), 'UI Group')
            self.assertEqual(self.change(group).status_code, 302)
            self.assertEqual(self.client.get(reverse('assets:depreciation-plan', args=[self.assets[2].pk])).status_code, 403)
        self.user.profile.interface_mode = 'lite'
        self.user.profile.save()
        self.assertContains(self.client.get(url), 'UI Group')
        self.client.force_login(self.admin)
        session = self.client.session
        session['lupine_ui_mode'] = 'lite'
        session.save()
        self.assertContains(self.client.get(url), 'UI Group')

    def test_search_has_bounded_results_and_no_hidden_assets(self):
        group = self.group()
        response = self.client.get(reverse('assets:collection-detail', args=[group.pk]), {'q': 'GUI-3'})
        self.assertContains(response, self.assets[3].barcode)
        self.assertNotContains(response, self.secret.barcode)
        self.assertEqual(len(response.context['candidates']), 1)
        self.assertFalse(response.context['form'].is_bound)

    def test_group_form_rejects_set_kind_and_set_has_separate_management(self):
        url = reverse('assets:group-create', args=[self.assets[0].pk])
        response = self.client.post(url, dict(kind='set', name='Invalid', request_id=str(uuid.uuid4()), asset_ids=[self.assets[1].pk]))
        self.assertContains(response, 'Ten formularz tworzy grupę')
        self.assertEqual(AssetCollection.objects.count(), 0)
        collection = self.strict_set()
        self.assertEqual(self.change(collection, ids=[self.assets[3].pk]).status_code, 302)

    def test_set_card_has_effective_number_without_overwriting_original(self):
        self.strict_set()
        response = self.client.get(reverse('assets:detail', args=[self.assets[1].pk]))
        self.assertContains(response, 'Numer dziedziczony z zestawu')
        self.assertEqual(response.context['collection_info']['effective_number'], self.assets[0].inventory_number)
        self.assets[1].refresh_from_db()
        self.assertEqual(self.assets[1].inventory_number, 'UI-INV-1')

    def test_component_cannot_acquire_value_quantity_or_own_number_through_form(self):
        self.strict_set()
        asset = self.assets[1]
        data = model_to_dict(asset)
        data['location_fk'] = asset.location_fk_id
        data.update(purchase_value='99', inventory_number='OVERWRITE', current_quantity=2)
        form = AssetForm(data=data, instance=asset)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        asset.refresh_from_db()
        self.assertEqual(asset.inventory_number, 'UI-INV-1')
        self.assertIsNone(asset.purchase_value)
        self.assertEqual(asset.current_quantity, 1)

    def test_model_guard_blocks_component_plan_and_value_even_outside_form(self):
        self.strict_set()
        child = self.assets[1]
        child.purchase_value = Decimal('100')
        with self.assertRaises(ValidationError):
            child.save()
        with self.assertRaises(ValidationError):
            AssetDepreciationPlan.objects.create(asset=child, enabled=False)
        self.assertEqual(AssetDepreciationPlan.objects.count(), 0)

    def test_set_individual_withdrawal_rollback_and_parent_move_blocked(self):
        self.strict_set()
        from datetime import date
        with self.assertRaises(ValidationError):
            withdraw_asset(asset_id=self.assets[1].pk, operator=self.user,
                data=dict(request_id=uuid.uuid4(), expected_quantity=1, method='sale',
                    action_date=date.today(), notes='', document_number=''))
        response = self.client.post(reverse('assets:api-bulk-move'),
            data={'asset_ids': [self.assets[0].pk], 'target_location_id': self.room.pk}, content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.assets[1].refresh_from_db()
        self.assertTrue(self.assets[1].is_active)

    def test_physical_component_move_allowed_and_plan_link_returns_to_card(self):
        self.strict_set()
        self.client.force_login(self.admin)
        response = self.client.post(reverse('assets:api-bulk-move'),
            data={'asset_ids': [self.assets[1].pk], 'target_location_id': self.other_room.pk}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assets[1].refresh_from_db()
        self.assertEqual(self.assets[1].location_fk_id, self.other_room.pk)
        self.assertEqual(self.client.get(reverse('assets:depreciation-plan', args=[self.assets[1].pk])).status_code, 302)

    def test_hidden_set_parent_details_not_leaked_on_child_card(self):
        self.strict_set()
        Asset.objects.filter(pk=self.assets[0].pk).update(location_fk=self.other_room)
        context = collection_context(self.assets[1], self.user)
        self.assertIsNone(context['parent'])
        self.assertNotEqual(context['effective_number'], self.assets[0].inventory_number)
        response = self.client.get(reverse('assets:detail', args=[self.assets[1].pk]))
        self.assertNotContains(response, self.assets[0].barcode)

    def test_list_api_keeps_equal_group_rows_and_adds_scoped_link_metadata(self):
        group = self.group()
        response = self.client.get(reverse('assets:api-list'))
        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.json()['results']}
        self.assertEqual(len(rows), 4)
        self.assertNotIn(self.secret.pk, rows)
        self.assertEqual(rows[self.assets[0].pk]['collection']['id'], group.pk)
        self.assertEqual(rows[self.assets[0].pk]['collection']['kind'], 'group')
        self.assertEqual(rows[self.assets[0].pk]['inventory_number'], 'UI-INV-0')
        self.assertNotIn('collection', rows[self.assets[3].pk])

    def test_get_card_and_group_pages_do_not_change_history_or_versions(self):
        group = self.group()
        before = AssetCollectionEvent.objects.count()
        from .models import AssetHistoryEntry
        old_history = AssetHistoryEntry.objects.count()
        for _ in range(2):
            self.client.get(reverse('assets:detail', args=[self.assets[0].pk]))
            self.client.get(reverse('assets:collection-detail', args=[group.pk]))
        self.assertEqual(AssetCollectionEvent.objects.count(), before)
        self.assertEqual(AssetHistoryEntry.objects.count(), old_history)
        group.refresh_from_db()
        self.assertEqual(group.version, 1)

    def test_adding_one_group_member_does_not_duplicate_every_members_history(self):
        group = self.group()
        from .models import AssetHistoryEntry
        before = AssetHistoryEntry.objects.count()
        self.assertEqual(self.change(group).status_code, 302)
        self.assertEqual(AssetHistoryEntry.objects.count(), before + 1)
        self.assertEqual(AssetCollectionEvent.objects.latest('pk').before,
            [{'asset_id': asset.pk, 'role': 'member'} for asset in self.assets[:2]])

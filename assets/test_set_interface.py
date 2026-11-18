import uuid
from decimal import Decimal
from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from users.models import User
from locations.models import Location
from .models import Asset, AssetCollection, AssetCollectionEvent, AssetWithdrawal, AssetHistoryEntry
from .collections import create_collection, operate_set


class SetInterfaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='set-ui-user')
        self.room = Location.objects.create(name='Set UI office', code='SUI-ROOM')
        self.target = Location.objects.create(name='Set UI target', code='SUI-TARGET')
        self.secret_room = Location.objects.create(name='Set secret room', code='SUI-SECRET')
        self.user.profile.allowed_locations.add(self.room, self.target)
        self.assets = [Asset.objects.create(name=f'Set UI {i}', barcode=f'SUI-{i}', inventory_number=f'SUI-INV-{i}',
            location_fk=self.room, asset_type='fixed') for i in range(4)]
        self.client.force_login(self.user)

    def create(self):
        return create_collection(operator=self.user, kind='set', name='Set', parent_id=self.assets[0].pk,
            asset_ids=[a.pk for a in self.assets[:3]], request_id=uuid.uuid4())

    def asset_data(self, prefix=''):
        return {prefix+k: v for k,v in dict(name='New set asset', barcode='SUI-NEW', inventory_number='NEW/OWN',
            location_fk=self.room.pk, asset_type='fixed', current_quantity=1,
            status='active', technical_condition='good').items()}

    def test_create_set_with_existing_parent(self):
        url = reverse('assets:set-create', args=[self.assets[0].pk])
        self.assertContains(self.client.get(url), 'Nowy środek nadrzędny')
        data = dict(parent_mode='existing', request_id=str(uuid.uuid4()), asset_ids=[self.assets[1].pk])
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        collection = AssetCollection.objects.get()
        self.assertEqual(collection.kind, 'set')
        self.assertEqual(collection.memberships.count(), 2)

    def test_create_set_generates_parent_atomically_and_replay_creates_nothing(self):
        url = reverse('assets:set-create', args=[self.assets[0].pk])
        data = dict(parent_mode='new', request_id=str(uuid.uuid4()), **self.asset_data('parent-'))
        before = Asset.objects.count()
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(Asset.objects.count(), before + 1)
        collection = AssetCollection.objects.get()
        parent = collection.memberships.get(role='parent').asset
        self.assertEqual(parent.name, 'New set asset')
        self.assertTrue(AssetHistoryEntry.objects.filter(asset=parent, event_type='created').exists())
        self.assertEqual(collection.memberships.get(role='member').asset_id, self.assets[0].pk)

    def test_failed_new_parent_with_valued_existing_child_leaves_no_orphan(self):
        self.assets[0].purchase_value = Decimal('500')
        self.assets[0].save()
        response = self.client.post(reverse('assets:set-create', args=[self.assets[0].pk]),
            dict(parent_mode='new', request_id=str(uuid.uuid4()), **self.asset_data('parent-')))
        self.assertContains(response, 'własną wartością')
        self.assertFalse(Asset.objects.filter(barcode='SUI-NEW').exists())
        self.assertFalse(AssetCollection.objects.exists())

    def test_create_child_and_retry_are_one_atomic_operation(self):
        collection = self.create()
        url = reverse('assets:collection-member-create', args=[collection.pk])
        data = dict(action='attach', request_id=str(uuid.uuid4()), expected_version=1, **self.asset_data())
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(Asset.objects.filter(barcode='SUI-0/1', inventory_number='SUI-INV-0').count(), 1)
        self.assertEqual(collection.memberships.filter(left_at__isnull=True).count(), 4)

    def test_inherited_fields_ignore_tampering_and_stale_child_is_not_saved(self):
        collection = self.create()
        url = reverse('assets:collection-member-create', args=[collection.pk])
        data = dict(action='attach', request_id=str(uuid.uuid4()), expected_version=1,
            purchase_value='120', **self.asset_data())
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Asset.objects.get(barcode='SUI-0/1').purchase_value)
        self.assertFalse(Asset.objects.filter(barcode='SUI-NEW').exists())
        data.pop('purchase_value')
        data['expected_version'] = 99
        data['request_id'] = str(uuid.uuid4())
        response = self.client.post(url, data)
        self.assertContains(response, 'Skład zmienił się')
        self.assertFalse(Asset.objects.filter(barcode='SUI-NEW').exists())

    def test_hierarchy_has_parent_only_and_children_paginate_separately(self):
        self.create()
        response = self.client.get(reverse('assets:api-list'), {'hierarchy': 1, 'page_size': 1, 'ordering':'name'})
        self.assertTrue(response.json()['hierarchy'])
        self.assertEqual(response.json()['pagination']['total_items'], 2)
        self.assertEqual(response.json()['results'][0]['id'], self.assets[0].pk)
        children = self.client.get(reverse('assets:api-list'), {'child_of': self.assets[0].pk, 'page_size':1})
        self.assertEqual(children.json()['pagination']['total_items'], 2)
        self.assertTrue(children.json()['pagination']['has_next'])
        self.assertEqual(children.json()['results'][0]['inventory_number'], 'SUI-INV-0')
        self.assertNotEqual(children.json()['results'][0]['own_inventory_number'], 'SUI-INV-0')

    def test_search_child_returns_parent_context_and_matching_child(self):
        self.create()
        query = {'hierarchy':1, 'search':self.assets[2].barcode}
        response = self.client.get(reverse('assets:api-list'), query)
        rows = response.json()['results']
        self.assertEqual([row['id'] for row in rows], [self.assets[0].pk])
        self.assertTrue(rows[0]['context_only'])
        response = self.client.get(reverse('assets:api-list'), dict(query, child_of=self.assets[0].pk))
        self.assertEqual([row['id'] for row in response.json()['results']], [self.assets[2].pk])

    def test_search_inherited_number_returns_all_children(self):
        self.create()
        response = self.client.get(reverse('assets:api-list'), {'child_of':self.assets[0].pk, 'search':'SUI-INV-0'})
        self.assertEqual(response.json()['pagination']['total_items'], 2)

    def test_hidden_parent_does_not_hide_children_or_leak_its_code(self):
        self.create()
        Asset.objects.filter(pk=self.assets[0].pk).update(location_fk=self.secret_room)
        response = self.client.get(reverse('assets:api-list'), {'hierarchy':1})
        rows = response.json()['results']
        self.assertEqual(len(rows), 3)
        self.assertNotIn(self.assets[0].pk, [row['id'] for row in rows])
        self.assertEqual(self.client.get(reverse('assets:api-list'), {'child_of':self.assets[0].pk}).status_code, 404)

    def test_csv_includes_children_regardless_of_hierarchy_and_preserves_own_number(self):
        self.create()
        response = self.client.get(reverse('assets:api-export'), dict(hierarchy=1,
            columns='inventory_number,own_inventory_number,collection_kind,collection_id,collection_role,barcode'))
        self.assertEqual(response.status_code, 200)
        content = b''.join(response.streaming_content).decode('utf-8') if response.streaming else response.content.decode('utf-8')
        self.assertIn('SUI-INV-0;SUI-INV-1;Zestaw', content)
        self.assertIn('SUI-2', content)

    def test_full_move_acts_on_all_members_and_rejects_stale_or_outside_target(self):
        collection = self.create()
        url = reverse('assets:set-operation', args=[collection.pk, 'move'])
        data = dict(request_id=str(uuid.uuid4()), expected_version=1, target_location=self.target.pk)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(Asset.objects.filter(pk__in=[a.pk for a in self.assets[:3]], location_fk=self.target).count(), 3)
        stale = dict(data, request_id=str(uuid.uuid4()), target_location=self.room.pk)
        self.assertContains(self.client.post(url, stale), 'Stan zestawu zmienił się')
        self.assertEqual(AssetCollectionEvent.objects.count(), 2)

    def test_full_withdrawal_and_restore_keep_composition_and_are_idempotent(self):
        collection = self.create()
        url = reverse('assets:set-operation', args=[collection.pk, 'withdraw'])
        page = self.client.get(url)
        self.assertEqual(page.content.count(b'name="request_id"'), 1)
        data = dict(request_id=str(uuid.uuid4()), expected_version=1, method='liquidation',
            action_date='2026-11-17', notes='Test', document_number='LT-TEST')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(AssetWithdrawal.objects.count(), 3)
        self.assertEqual(collection.memberships.filter(left_at__isnull=True).count(), 3)
        self.assertEqual(Asset.objects.filter(pk__in=[a.pk for a in self.assets[:3]], is_active=False).count(), 3)
        with self.settings(LUPINE_EDITION='lite'):
            self.assertEqual(self.client.get(reverse('assets:collection-detail', args=[collection.pk])).status_code, 404)
            self.assertEqual(self.client.post(reverse('assets:set-operation', args=[collection.pk,'restore']), {}).status_code, 403)
        restore = reverse('assets:set-operation', args=[collection.pk,'restore'])
        data = dict(request_id=str(uuid.uuid4()), expected_version=2)
        self.assertEqual(self.client.post(restore, data).status_code, 302)
        self.assertEqual(self.client.post(restore, data).status_code, 302)
        self.assertEqual(Asset.objects.filter(pk__in=[a.pk for a in self.assets[:3]], is_active=True).count(), 3)

    def test_state_operation_requires_whole_scope(self):
        collection = self.create()
        Asset.objects.filter(pk=self.assets[2].pk).update(location_fk=self.secret_room)
        url = reverse('assets:set-operation', args=[collection.pk,'withdraw'])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_failed_whole_withdrawal_rolls_back_everything_and_revokes_capability(self):
        collection = self.create()
        from .withdrawals import withdraw_asset
        calls = []
        def fail_second(**kwargs):
            calls.append(kwargs['asset_id'])
            if len(calls) == 2:
                raise ValidationError('Simulated failure')
            return withdraw_asset(**kwargs)
        with patch('assets.withdrawals.withdraw_asset', side_effect=fail_second), self.assertRaises(ValidationError):
            operate_set(operator=self.user, collection_id=collection.pk, action='withdraw', expected_version=1,
                request_id=uuid.uuid4(), data=dict(method='liquidation',action_date='2026-11-17',notes='',document_number=''))
        self.assertEqual(AssetWithdrawal.objects.count(), 0)
        self.assertEqual(Asset.objects.filter(is_active=False).count(), 0)
        with self.assertRaises(ValidationError):
            withdraw_asset(asset_id=self.assets[1].pk, operator=self.user, data=dict(request_id=uuid.uuid4(),
                expected_quantity=1, method='liquidation', action_date=date(2026,11,17),notes='',document_number=''))

    def test_dissolve_zero_value_set_preserves_assets_and_source_numbers(self):
        collection = self.create()
        before = list(Asset.objects.order_by('pk').values())
        response = self.client.post(reverse('assets:set-operation', args=[collection.pk,'dissolve']),
            dict(request_id=str(uuid.uuid4()), expected_version=1))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Asset.objects.order_by('pk').values()), before)
        self.assertEqual(collection.memberships.filter(left_at__isnull=True).count(), 0)
        self.assertEqual(collection.memberships.count(), 3)

    def test_dissolve_valued_set_requires_financial_resolution(self):
        collection = self.create()
        self.assets[0].purchase_value = Decimal('900')
        self.assets[0].save()
        response = self.client.post(reverse('assets:set-operation', args=[collection.pk,'dissolve']),
            dict(request_id=str(uuid.uuid4()), expected_version=1))
        self.assertContains(response, 'uzgodnienia wartości')
        self.assertEqual(collection.memberships.filter(left_at__isnull=True).count(), 3)

    def test_set_size_limit_is_atomic_and_does_not_limit_grouping_operations(self):
        collection = self.create()
        candidates = Asset.objects.bulk_create([Asset(name=f'Extra {i}', barcode=f'SUI-EXTRA-{i}',
            asset_type='fixed', location_fk=self.room) for i in range(198)])
        response = self.client.post(reverse('assets:collection-detail', args=[collection.pk]),
            dict(action='attach', expected_version=1, request_id=str(uuid.uuid4()), asset_ids=[a.pk for a in candidates]))
        self.assertContains(response, 'maksymalnie 200 kartotek')
        self.assertEqual(collection.memberships.filter(left_at__isnull=True).count(), 3)
        self.assertEqual(AssetCollectionEvent.objects.count(), 1)

    def test_lite_can_create_label_and_withdraw_set_without_redirecting_to_hidden_archive(self):
        with self.settings(LUPINE_EDITION='lite'):
            response = self.client.post(reverse('assets:set-create', args=[self.assets[0].pk]),
                dict(parent_mode='existing', request_id=str(uuid.uuid4()), asset_ids=[self.assets[1].pk]))
            self.assertEqual(response.status_code, 302)
            collection = AssetCollection.objects.get()
            labels = self.client.get(reverse('assets:labels-pdf'), {'ids':str(self.assets[1].pk)})
            self.assertEqual(labels.status_code, 200)
            self.assertTrue(b''.join(labels.streaming_content).startswith(b'%PDF'))
            self.assertEqual(self.client.get(reverse('assets:depreciation-plan', args=[self.assets[0].pk])).status_code, 403)
            response = self.client.post(reverse('assets:set-operation', args=[collection.pk,'withdraw']),
                dict(request_id=str(uuid.uuid4()), expected_version=1, method='sale', action_date='2026-11-17'))
            self.assertRedirects(response, reverse('assets:list'), fetch_redirect_response=False)
            self.assertEqual(self.client.get(reverse('assets:collection-detail', args=[collection.pk])).status_code, 404)

    def test_lt_document_contains_parent_once_when_parent_and_parts_selected(self):
        collection = self.create()
        operate_set(operator=self.user, collection_id=collection.pk, action='withdraw',
            expected_version=1, request_id=uuid.uuid4(), data=dict(method='liquidation',
                action_date='2026-11-17', notes='', document_number=''))
        ids = ','.join(str(asset.pk) for asset in self.assets[:3])
        response = self.client.get(reverse('assets:archive-lt'), {'ids':ids})
        self.assertEqual([asset.pk for asset in response.context['assets']], [self.assets[0].pk])
        self.assertEqual(response.context['total_quantity'], 1)
        response = self.client.post(reverse('assets:lt-generate'), dict(ids=ids,
            date_of_action='2026-11-17', format='json'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        from .models import LtDocument, AssetAttachment
        self.assertEqual(LtDocument.objects.get().asset_ids, [self.assets[0].pk])
        self.assertEqual(AssetAttachment.objects.filter(lt_document__isnull=False).count(), 1)

    def test_lt_rejects_component_alone_without_creating_document_or_sequence(self):
        collection = self.create()
        operate_set(operator=self.user, collection_id=collection.pk, action='withdraw',
            expected_version=1, request_id=uuid.uuid4(), data=dict(method='liquidation',
                action_date='2026-11-17', notes='', document_number=''))
        data = dict(ids=str(self.assets[1].pk), date_of_action='2026-11-17', format='json')
        self.assertEqual(self.client.get(reverse('assets:archive-lt'), {'ids':data['ids']}).status_code, 400)
        response = self.client.post(reverse('assets:lt-generate'), data)
        self.assertEqual(response.status_code, 400)
        self.assertIn('środek nadrzędny', response.json()['error'])
        from .models import LtDocument
        self.assertFalse(LtDocument.objects.exists())

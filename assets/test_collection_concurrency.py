from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
import uuid

from django.core.exceptions import ValidationError
from django.db import connection, connections, close_old_connections
from django.test import TransactionTestCase

from users.models import User
from locations.models import Location
from .models import Asset, AssetCollection, AssetCollectionEvent, AssetCollectionMembership, AssetDepreciationPlan
from .collections import create_collection, change_collection_members


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locking')
class CollectionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='set-concurrent')
        self.room = Location.objects.create(name='Concurrent', code='CONC-ROOM')
        self.assets = [Asset.objects.create(name=f'Concurrent {i}', barcode=f'CONC-{i}',
            asset_type='fixed', location_fk=self.room) for i in range(4)]

    def parallel(self, *operations):
        barrier = Barrier(len(operations))
        def worker(operation):
            close_old_connections()
            try:
                user = User.objects.get(pk=self.user.pk)
                barrier.wait(timeout=10)
                operation(user)
                return 'ok'
            except ValidationError:
                return 'conflict'
            finally:
                connections['default'].close()
        with ThreadPoolExecutor(max_workers=len(operations)) as executor:
            return list(executor.map(worker, operations))

    def test_two_creates_have_exactly_one_winner(self):
        def create(user):
            create_collection(operator=user, kind='group', name='Concurrent group',
                asset_ids=[a.pk for a in self.assets[:2]], request_id=uuid.uuid4())
        self.assertCountEqual(self.parallel(create, create), ['ok', 'conflict'])
        self.assertEqual(AssetCollection.objects.count(), 1)
        self.assertEqual(AssetCollectionMembership.objects.count(), 2)

    def component_post(self, user, key, collection=None):
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from .collection_views import collection_member_create
        path = '/assets/collections/%s/member/create/' % collection.pk if collection else '/assets/%s/set/component/create/' % self.assets[0].pk
        request = RequestFactory().post(path, dict(action='attach', expected_version=1,
            request_id=str(key), name='New component', asset_type='',
            location_fk=self.room.pk, status='active', technical_condition='good'))
        request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        response = collection_member_create(request, pk=collection.pk) if collection else collection_member_create(request, asset_id=self.assets[0].pk)
        if response.status_code != 302:
            raise ValidationError('Conflict')

    def test_parallel_first_component_creates_only_one_set(self):
        def create(user):
            self.component_post(user, uuid.uuid4())
        self.assertCountEqual(self.parallel(create, create), ['ok', 'conflict'])
        self.assertEqual(AssetCollection.objects.count(), 1)
        self.assertEqual(Asset.objects.filter(barcode='CONC-0/1').count(), 1)
        self.assertEqual(Asset.objects.count(), 5)

    def test_parallel_first_component_retry_is_idempotent(self):
        key = uuid.uuid4()
        def create(user):
            self.component_post(user, key)
        self.assertEqual(self.parallel(create, create), ['ok', 'ok'])
        self.assertEqual(AssetCollectionEvent.objects.count(), 1)
        self.assertEqual(Asset.objects.filter(barcode='CONC-0/1').count(), 1)

    def test_parallel_component_addition_rejects_stale_form_without_orphan(self):
        collection = create_collection(operator=self.user, kind='set', name='Set', parent_id=self.assets[0].pk,
            asset_ids=[a.pk for a in self.assets[:2]], request_id=uuid.uuid4())
        def create(user):
            self.component_post(user, uuid.uuid4(), collection)
        self.assertCountEqual(self.parallel(create, create), ['ok', 'conflict'])
        self.assertEqual(Asset.objects.count(), 5)
        self.assertEqual(Asset.objects.filter(barcode='CONC-0/1').count(), 1)

    def test_same_asset_cannot_join_two_groups_concurrently(self):
        groups = [create_collection(operator=self.user, kind='group', name=f'Group {i}',
            asset_ids=[self.assets[i].pk], request_id=uuid.uuid4()) for i in range(2)]
        def attach(group):
            return lambda user: change_collection_members(operator=user, collection_id=group.pk,
                action='attach', asset_ids=[self.assets[2].pk], expected_version=1, request_id=uuid.uuid4())
        self.assertCountEqual(self.parallel(*[attach(group) for group in groups]), ['ok', 'conflict'])
        self.assertEqual(AssetCollectionMembership.objects.filter(asset=self.assets[2], left_at__isnull=True).count(), 1)

    def test_parallel_identical_retry_is_one_event(self):
        group = create_collection(operator=self.user, kind='group', name='Retry group',
            asset_ids=[self.assets[0].pk], request_id=uuid.uuid4())
        key = uuid.uuid4()
        def attach(user):
            change_collection_members(operator=user, collection_id=group.pk, action='attach',
                asset_ids=[self.assets[1].pk], expected_version=1, request_id=key)
        self.assertEqual(self.parallel(attach, attach), ['ok', 'ok'])
        self.assertEqual(AssetCollectionEvent.objects.count(), 2)
        group.refresh_from_db()
        self.assertEqual(group.version, 2)

    def create_set(self, user):
        create_collection(operator=user, kind='set', name='Safe set', parent_id=self.assets[0].pk,
            asset_ids=[a.pk for a in self.assets[:2]], request_id=uuid.uuid4())

    def test_component_value_edit_serializes_with_set_creation(self):
        def edit(user):
            asset = Asset.objects.get(pk=self.assets[1].pk)
            asset.purchase_value = 100
            asset.save()
        self.assertCountEqual(self.parallel(self.create_set, edit), ['ok', 'conflict'])
        self.assets[1].refresh_from_db()
        self.assertFalse(AssetCollection.objects.filter(kind='set').exists() and self.assets[1].purchase_value)

    def test_component_plan_creation_serializes_with_set_creation(self):
        def plan(user):
            AssetDepreciationPlan.objects.create(asset_id=self.assets[1].pk, enabled=False)
        self.assertCountEqual(self.parallel(self.create_set, plan), ['ok', 'conflict'])
        self.assertFalse(AssetCollection.objects.filter(kind='set').exists() and AssetDepreciationPlan.objects.exists())

    def test_legacy_inventory_apply_race_cannot_zero_new_set(self):
        from inventory.models import InventorySession, InventorySnapshotItem
        from inventory.views import apply_inventory_session_to_assets
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        session = InventorySession.objects.create(number='INV-OLD-RACE', created_by=self.user, status='closed')
        for asset in self.assets[:2]:
            InventorySnapshotItem.objects.create(session=session, asset=asset, asset_id_snapshot=asset.pk,
                inventory_number='', name=asset.name, barcode=asset.barcode, asset_type='fixed',
                record_quantity_snapshot=1, location_fk_id_snapshot=self.room.pk)
        def apply(user):
            request = RequestFactory().post('/inventory/apply/')
            request.user = user
            request.session = {}
            request._messages = FallbackStorage(request)
            apply_inventory_session_to_assets(request, session.pk)
        self.parallel(self.create_set, apply)
        session.refresh_from_db()
        if AssetCollection.objects.filter(kind='set').exists():
            self.assertIsNone(session.applied_to_assets_at)
            self.assertEqual(Asset.objects.filter(pk__in=[a.pk for a in self.assets[:2]], current_quantity=1).count(), 2)
        else:
            self.assertIsNotNone(session.applied_to_assets_at)
            self.assertEqual(Asset.objects.filter(pk__in=[a.pk for a in self.assets[:2]], current_quantity=0).count(), 2)

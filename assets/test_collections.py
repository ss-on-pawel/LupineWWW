import uuid
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.forms.models import model_to_dict
from django.test import TestCase
from django.utils import timezone

from users.models import User
from locations.models import Location
from inventory.models import InventorySession, InventorySnapshotItem
from inventory.services import start_inventory_session
from .models import Asset, AssetCollection, AssetCollectionMembership, AssetCollectionEvent, AssetDepreciationPlan, AssetHistoryEntry
from .collections import create_collection, change_collection_members, effective_inventory_numbers
from .report_utils import build_depreciation_report, build_depreciation_report_full_year


class AssetCollectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='collection-admin')
        self.room = Location.objects.create(name='Office', code='COL-ROOM')
        self.assets = [Asset.objects.create(name=f'Part {i}', barcode=f'COL-{i}', inventory_number=f'INV-{i}',
            location_fk=self.room, asset_type='fixed') for i in range(5)]

    def create(self, kind='group', assets=None, **kwargs):
        ids = [a.pk for a in (assets or self.assets[:3])]
        if kind == 'set':
            kwargs.setdefault('parent_id', ids[0])
        return create_collection(operator=self.user, kind=kind, name='Desk', asset_ids=ids,
            request_id=kwargs.pop('request_id', uuid.uuid4()), **kwargs)

    def change(self, collection, action, assets, **kwargs):
        return change_collection_members(operator=kwargs.pop('operator', self.user), collection_id=collection.pk,
            asset_ids=[a.pk for a in assets], action=action,
            expected_version=kwargs.pop('expected_version', collection.version), request_id=kwargs.pop('request_id', uuid.uuid4()), **kwargs)

    def test_group_preserves_all_asset_fields_numbers_and_codes(self):
        before = [model_to_dict(a) for a in self.assets]
        group = self.create()
        self.assertEqual(group.kind, 'group')
        self.assertEqual(group.memberships.count(), 3)
        self.assertEqual(AssetHistoryEntry.objects.filter(field_name='collection').count(), 3)
        for asset, old in zip(self.assets, before):
            asset.refresh_from_db()
            self.assertEqual(model_to_dict(asset), old)
        self.assertEqual(effective_inventory_numbers(self.assets), {a.pk: a.inventory_number for a in self.assets})

    def test_set_resolves_parent_number_without_overwriting_source(self):
        collection = self.create('set')
        self.assertEqual(collection.name, self.assets[0].name)
        numbers = effective_inventory_numbers(self.assets)
        self.assertEqual(numbers[self.assets[1].pk], 'INV-0')
        self.assets[1].refresh_from_db()
        self.assertEqual(self.assets[1].inventory_number, 'INV-1')
        self.assets[0].inventory_number = 'CHANGED'
        self.assets[0].save()
        with self.assertNumQueries(2):
            self.assertEqual(effective_inventory_numbers(self.assets)[self.assets[1].pk], 'CHANGED')

    def test_detach_keeps_membership_history_and_restores_own_number(self):
        collection = self.create('set')
        collection = self.change(collection, 'detach', [self.assets[1]])
        old = collection.memberships.get(asset=self.assets[1])
        self.assertIsNotNone(old.left_at)
        self.assertEqual(old.left_by, self.user)
        self.assertEqual(effective_inventory_numbers(self.assets)[self.assets[1].pk], 'INV-1')
        self.change(collection, 'attach', [self.assets[1]])
        self.assertEqual(collection.memberships.filter(asset=self.assets[1]).count(), 2)
        self.assertEqual(AssetCollectionEvent.objects.count(), 3)

    def test_create_and_change_retries_are_idempotent(self):
        key = uuid.uuid4()
        collection = self.create(request_id=key)
        replay = self.create(request_id=key)
        self.assertEqual(collection.pk, replay.pk)
        attach_key = uuid.uuid4()
        changed = self.change(collection, 'attach', [self.assets[3]], request_id=attach_key)
        replay = self.change(collection, 'attach', [self.assets[3]], request_id=attach_key)
        self.assertEqual(changed.version, replay.version)
        self.assertEqual(AssetCollectionEvent.objects.count(), 2)
        self.assertEqual(AssetCollectionMembership.objects.count(), 4)

    def test_reused_request_id_with_different_data_is_rejected(self):
        key = uuid.uuid4()
        self.create(request_id=key)
        with self.assertRaises(ValidationError):
            self.create(assets=self.assets[2:], request_id=key)

    def test_stale_version_has_no_partial_writes(self):
        collection = self.create()
        changed = self.change(collection, 'attach', [self.assets[3]])
        with self.assertRaises(ValidationError):
            self.change(collection, 'detach', [self.assets[1]])
        self.assertEqual(changed.memberships.filter(left_at__isnull=True).count(), 4)
        self.assertEqual(AssetCollectionEvent.objects.count(), 2)

    def test_active_membership_exclusive_even_via_bulk_create(self):
        collection = self.create()
        other = AssetCollection.objects.create(kind='group', name='Other', created_by=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetCollectionMembership.objects.bulk_create([AssetCollectionMembership(collection=other,
                asset=self.assets[1], joined_by=self.user)])
        with self.assertRaises(ValidationError):
            self.create(assets=[self.assets[1], self.assets[4]])
        self.assertEqual(collection.memberships.count(), 3)

    def test_only_one_active_parent_at_database_level(self):
        collection = self.create('set')
        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetCollectionMembership.objects.create(collection=collection, asset=self.assets[4], role='parent', joined_by=self.user)

    def test_historical_links_protect_assets_from_physical_delete(self):
        collection = self.create()
        self.change(collection, 'detach', [self.assets[1]])
        with self.assertRaises(ProtectedError):
            self.assets[1].delete()

    def test_forbidden_archived_and_unknown_assets_are_atomic(self):
        restricted = User.objects.create_user(username='collection-restricted')
        with self.assertRaises(PermissionDenied):
            create_collection(operator=restricted, kind='group', name='Secret', asset_ids=[self.assets[0].pk], request_id=uuid.uuid4())
        restricted.profile.allowed_locations.add(self.room)
        group = create_collection(operator=restricted, kind='group', name='Allowed', asset_ids=[self.assets[0].pk], request_id=uuid.uuid4())
        self.assertEqual(group.created_by, restricted)
        self.assets[4].status = Asset.Status.LIQUIDATED
        self.assets[4].save()
        with self.assertRaises(ValidationError):
            self.change(group, 'attach', [self.assets[3], self.assets[4]])
        self.assertEqual(group.memberships.count(), 1)

    def test_change_requires_access_to_whole_collection_not_only_selection(self):
        collection = self.create()
        restricted = User.objects.create_user(username='collection-limited')
        restricted.profile.allowed_locations.add(self.room)
        other_room = Location.objects.create(name='Other', code='COL-OTHER')
        Asset.objects.filter(pk=self.assets[2].pk).update(location_fk=other_room)
        with self.assertRaises(PermissionDenied):
            self.change(collection, 'detach', [self.assets[0]], operator=restricted)

    def test_set_rejects_cycles_parent_removal_and_last_child_removal(self):
        collection = self.create('set', assets=self.assets[:2])
        with self.assertRaises(ValidationError):
            self.change(collection, 'detach', [self.assets[0]])
        with self.assertRaises(ValidationError):
            self.change(collection, 'detach', [self.assets[1]])
        with self.assertRaises(ValidationError):
            self.create('set', assets=[self.assets[3], self.assets[0]])

    def test_set_rejects_own_value_and_even_disabled_depreciation_plan(self):
        self.assets[1].purchase_value = Decimal('120')
        self.assets[1].save()
        with self.assertRaises(ValidationError):
            self.create('set')
        self.assets[1].purchase_value = None
        self.assets[1].save()
        AssetDepreciationPlan.objects.create(asset=self.assets[1], enabled=False)
        with self.assertRaises(ValidationError):
            self.create('set')
        self.assertEqual(AssetCollection.objects.count(), 0)

    def test_set_rejects_multi_quantity_assets(self):
        self.assets[1].current_quantity = 2
        self.assets[1].save()
        with self.assertRaises(ValidationError):
            self.create('set')
        self.assertEqual(self.create().kind, 'group')

    def test_financial_detach_is_blocked_without_changing_values(self):
        collection = self.create('set')
        self.assets[0].purchase_value = Decimal('900')
        self.assets[0].save()
        with self.assertRaises(ValidationError):
            self.change(collection, 'detach', [self.assets[1]])
        self.assertEqual(collection.memberships.filter(left_at__isnull=True).count(), 3)
        self.assets[0].refresh_from_db()
        self.assertEqual(self.assets[0].purchase_value, Decimal('900'))

    def test_set_changes_blocked_during_active_inventory(self):
        start_inventory_session(created_by=self.user, root_locations=[self.room], asset_types=['fixed'])
        with self.assertRaises(ValidationError):
            self.create('set')
        self.assertEqual(self.create().kind, 'group')

    def test_group_does_not_change_existing_snapshot(self):
        session = start_inventory_session(created_by=self.user, root_locations=[self.room], asset_types=['fixed'])
        before = list(InventorySnapshotItem.objects.filter(session=session).order_by('pk').values())
        group = self.create()
        self.change(group, 'detach', [self.assets[1]])
        self.assertEqual(list(session.snapshot_items.order_by('pk').values()), before)

    def test_group_never_changes_past_depreciation_reports(self):
        from datetime import date
        plan = AssetDepreciationPlan.objects.create(asset=self.assets[1], enabled=True, method='linear',
            initial_value=Decimal('1200'), depreciation_start_date=date(2025, 1, 1),
            monthly_depreciation_amount=Decimal('10'), annual_depreciation_amount=Decimal('120'))
        old = model_to_dict(plan)
        reports = build_depreciation_report(2025), build_depreciation_report_full_year(2025)
        group = self.create()
        self.change(group, 'detach', [self.assets[1]])
        self.assertEqual((build_depreciation_report(2025), build_depreciation_report_full_year(2025)), reports)
        plan.refresh_from_db()
        self.assertEqual(model_to_dict(plan), old)

    def test_leave_timestamp_and_actor_pair_enforced_in_database(self):
        group = self.create()
        with self.assertRaises(IntegrityError), transaction.atomic():
            group.memberships.filter(asset=self.assets[1]).update(left_at=timezone.now())

    def test_bad_inputs_do_not_create_any_collections(self):
        for kwargs in [dict(kind='bad'), dict(name=''), dict(asset_ids=[True]), dict(parent_id=self.assets[0].pk), dict(request_id='bad')]:
            params = dict(operator=self.user, kind='group', name='Desk', asset_ids=[self.assets[0].pk], request_id=uuid.uuid4())
            params.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                create_collection(**params)
        self.assertEqual(AssetCollection.objects.count(), 0)

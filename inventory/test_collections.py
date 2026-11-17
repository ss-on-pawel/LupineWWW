import uuid
from unittest import skipUnless
from django.db import connection
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from users.models import User
from locations.models import Location
from assets.models import Asset
from assets.collections import create_collection, change_collection_members
from .models import InventorySession, InventorySnapshotItem
from .services import start_inventory_session, import_inventory_scan_text
from .views import _build_inventory_session_analysis


class CollectionInventoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='set-inventory-admin', password='test')
        self.room = Location.objects.create(name='Set room', code='SET-ROOM')
        self.other = Location.objects.create(name='Other set room', code='SET-OTHER')
        self.assets = [Asset.objects.create(name=f'Computer part {i}', inventory_number=f'SET-INV-{i}',
            barcode=f'SET-{i}', asset_type='fixed', location_fk=self.room) for i in range(3)]
        self.collection = create_collection(operator=self.user, kind='set', name='Computer',
            parent_id=self.assets[0].pk, asset_ids=[a.pk for a in self.assets], request_id=uuid.uuid4())
        self.client.force_login(self.user)

    def start(self):
        return start_inventory_session(created_by=self.user, root_locations=[self.room], asset_types=['fixed'])

    def scan(self, session, *codes):
        import_inventory_scan_text('\n'.join([session.number, self.room.code, *codes]), uploaded_by=self.user)

    def analysis(self, session):
        return _build_inventory_session_analysis(session, user=self.user)

    def test_snapshot_freezes_effective_number_and_full_composition(self):
        session = self.start()
        rows = list(session.snapshot_items.order_by('asset_id_snapshot'))
        self.assertEqual(len(rows), 3)
        self.assertEqual({row.inventory_number for row in rows}, {'SET-INV-0'})
        self.assertEqual(rows[0].collection_snapshot['role'], 'parent')
        self.assertEqual(rows[1].collection_snapshot['expected_ids'], [a.pk for a in self.assets[1:]])
        self.assertEqual(rows[1].purchase_value_snapshot, Decimal('0'))

    def test_parent_scan_and_manual_confirmation_cannot_confirm_components(self):
        session = self.start()
        self.scan(session, self.assets[0].barcode)
        analysis = self.analysis(session)
        self.assertEqual(analysis['snapshot_total'], 2)
        self.assertEqual(analysis['read_count'], 0)
        self.assertFalse(analysis['collection_summaries'][0]['complete'])
        response = self.client.post(reverse('inventory:manual-confirmation-api', args=[session.pk]),
            dict(asset_id=self.assets[0].pk, confirmed=True), content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_all_children_complete_set_without_parent_scan_and_report_has_one_parent(self):
        session = self.start()
        self.scan(session, *[a.barcode for a in self.assets[1:]])
        analysis = self.analysis(session)
        self.assertEqual(analysis['snapshot_total'], 2)
        self.assertEqual(analysis['matching_count'], 2)
        self.assertTrue(analysis['collection_summaries'][0]['complete'])
        response = self.client.get(reverse('inventory:collection-report', args=[session.pk]))
        self.assertContains(response, 'Kompletny')
        self.assertEqual(len(response.context['summaries']), 1)
        self.assertEqual(response.context['standalone'], [])

    def test_missing_child_visible_in_report_without_valuation_of_whole_set(self):
        self.assets[0].purchase_value = Decimal('1000')
        self.assets[0].save()
        session = self.start()
        self.scan(session, self.assets[1].barcode)
        response = self.client.get(reverse('inventory:collection-report', args=[session.pk]))
        self.assertContains(response, self.assets[2].name)
        self.assertContains(response, 'Niekompletny')
        self.assertContains(response, 'nie wycenia ich automatycznie')
        self.assertEqual(self.analysis(session)['shortage_count'], 1)

    def test_partial_scope_is_not_complete_and_cannot_be_applied(self):
        Asset.objects.filter(pk=self.assets[2].pk).update(location_fk=self.other)
        session = self.start()
        self.scan(session, self.assets[1].barcode)
        summary = self.analysis(session)['collection_summaries'][0]
        self.assertTrue(summary['partial'])
        self.assertFalse(summary['complete'])
        self.close(session)
        response = self.client.post(reverse('inventory:session-apply-to-assets', args=[session.pk]))
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertIsNone(session.applied_to_assets_at)

    def test_wrong_location_is_distinct_from_physical_completeness(self):
        session = self.start()
        import_inventory_scan_text('\n'.join([session.number, self.other.code, *[a.barcode for a in self.assets[1:]]]), uploaded_by=self.user)
        summary = self.analysis(session)['collection_summaries'][0]
        self.assertTrue(summary['complete'])
        self.assertFalse(summary['matching'])
        self.assertEqual(summary['status'], 'Kompletny — inna lokalizacja')

    def close(self, session):
        session.status = InventorySession.Status.CLOSED
        session.closed_at = timezone.now()
        session.save()

    def test_changed_live_composition_never_changes_frozen_report_and_blocks_apply(self):
        session = self.start()
        before = list(session.snapshot_items.order_by('pk').values())
        self.close(session)
        change_collection_members(operator=self.user, collection_id=self.collection.pk, action='detach',
            asset_ids=[self.assets[2].pk], expected_version=1, request_id=uuid.uuid4())
        self.assertEqual(list(session.snapshot_items.order_by('pk').values()), before)
        self.assertEqual(self.analysis(session)['collection_summaries'][0]['expected'], 2)
        self.client.post(reverse('inventory:session-apply-to-assets', args=[session.pk]))
        session.refresh_from_db()
        self.assertIsNone(session.applied_to_assets_at)

    def test_apply_missing_part_does_not_zero_accounting_parent(self):
        session = self.start()
        self.scan(session, self.assets[1].barcode)
        self.close(session)
        self.client.post(reverse('inventory:session-apply-to-assets', args=[session.pk]))
        session.refresh_from_db()
        self.assertIsNotNone(session.applied_to_assets_at)
        self.assets[0].refresh_from_db()
        self.assets[2].refresh_from_db()
        self.assertEqual(self.assets[0].current_quantity, 1)
        self.assertEqual(self.assets[0].last_inventory_quantity, 0)
        self.assertEqual(self.assets[2].current_quantity, 0)

    def test_partial_user_report_hides_unavailable_parent_name(self):
        session = self.start()
        restricted = User.objects.create_user(username='set-inventory-limited')
        restricted.profile.allowed_locations.add(self.other)
        # Restrict through snapshot scope, independently from current asset locations.
        session.snapshot_items.filter(asset_id_snapshot=self.assets[1].pk).update(location_fk_id_snapshot=self.other.pk)
        analysis = _build_inventory_session_analysis(session, user=restricted)
        self.assertTrue(analysis['collection_summaries'][0]['partial'])
        self.assertIsNone(analysis['collection_summaries'][0]['parent_item'])

    def test_old_session_is_not_reinterpreted_after_adding_set(self):
        old = InventorySession.objects.create(number='INV-OLD', created_by=self.user, status='closed')
        for asset in self.assets:
            InventorySnapshotItem.objects.create(session=old, asset=asset, asset_id_snapshot=asset.pk,
                inventory_number=asset.inventory_number, name=asset.name, barcode=asset.barcode,
                asset_type='fixed', location_fk_id_snapshot=self.room.pk, record_quantity_snapshot=1)
        analysis = self.analysis(old)
        self.assertEqual(analysis['snapshot_total'], 3)
        self.assertEqual(analysis['collection_summaries'], [])

    @skipUnless(connection.vendor == 'postgresql', 'PostgreSQL summary')
    def test_postgres_physical_summary_matches_python_including_parent_scan(self):
        session = self.start()
        self.scan(session, self.assets[0].barcode, self.assets[1].barcode)
        python = self.analysis(session)
        from .sql_analysis import inventory_summary
        with self.assertNumQueries(1):
            sql = inventory_summary(session, self.user)
        for key, value in sql.items():
            self.assertEqual(value, python[key], key)

    def test_missing_component_is_expected_in_next_inventory_and_can_recover(self):
        first = self.start()
        self.scan(first, self.assets[1].barcode)
        self.close(first)
        self.client.post(reverse('inventory:session-apply-to-assets', args=[first.pk]))
        second = self.start()
        self.scan(second, *[a.barcode for a in self.assets[1:]])
        self.assertTrue(self.analysis(second)['collection_summaries'][0]['complete'])
        self.close(second)
        self.client.post(reverse('inventory:session-apply-to-assets', args=[second.pk]))
        second.refresh_from_db()
        self.assertIsNotNone(second.applied_to_assets_at)
        self.assets[2].refresh_from_db()
        self.assertEqual(self.assets[2].current_quantity, 1)

    def test_legacy_closed_session_cannot_overwrite_asset_joined_to_new_set(self):
        old = InventorySession.objects.create(number='INV-LEGACY', created_by=self.user, status='closed')
        for asset in self.assets:
            InventorySnapshotItem.objects.create(session=old, asset=asset, asset_id_snapshot=asset.pk,
                inventory_number=asset.inventory_number, name=asset.name, barcode=asset.barcode,
                asset_type='fixed', location_fk_id_snapshot=self.room.pk, record_quantity_snapshot=1)
        self.client.post(reverse('inventory:session-apply-to-assets', args=[old.pk]))
        old.refresh_from_db()
        self.assertIsNone(old.applied_to_assets_at)
        self.assertEqual(Asset.objects.filter(pk__in=[a.pk for a in self.assets], current_quantity=1).count(), 3)

    def test_missing_component_does_not_prevent_adding_new_physical_part(self):
        session = self.start()
        self.close(session)
        self.client.post(reverse('inventory:session-apply-to-assets', args=[session.pk]))
        replacement = Asset.objects.create(name='Extra part', barcode='SET-EXTRA', asset_type='fixed', location_fk=self.room)
        change_collection_members(operator=self.user, collection_id=self.collection.pk, action='attach',
            asset_ids=[replacement.pk], expected_version=1, request_id=uuid.uuid4())
        self.assertEqual(self.collection.memberships.filter(left_at__isnull=True).count(), 4)

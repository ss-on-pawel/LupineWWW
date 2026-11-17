from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import User
from locations.models import Location
from assets.models import Asset
from .models import InventorySession, InventorySnapshotItem, InventoryScanBatch
from .services import start_inventory_session, _build_snapshot_item, _build_location_paths


class SnapshotScalingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='snapshot-admin')
        self.root = Location.objects.create(name='Góra ł')
        self.leaf = self.root
        for level in range(5):
            self.leaf = Location.objects.create(name=f'Pokój {level}', parent=self.leaf)

    def create_assets(self, count, offset=0):
        Asset.objects.bulk_create([Asset(name=f'Środek {i}', barcode=f'SCALE-{i}', asset_type='fixed',
                                        inventory_number=f'000/{i}', current_quantity=i % 4,
                                        purchase_value=Decimal('12.34'), location_fk=self.leaf)
                                   for i in range(offset, offset + count)])

    def start(self):
        return start_inventory_session(created_by=self.user, root_locations=[self.root], asset_types=['fixed'])

    def test_reads_do_not_grow_with_assets_and_snapshot_fields_match(self):
        self.create_assets(2)
        with CaptureQueriesContext(connection) as small:
            self.start()
        self.create_assets(30, offset=2)
        with CaptureQueriesContext(connection) as large:
            session = self.start()
        def selects(queries):
            return [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith('SELECT')]
        self.assertEqual(len(selects(small)), len(selects(large)))
        # One tree read; the M2M scope assignment also reads Location via a JOIN.
        self.assertEqual(sum('FROM "locations_location"' in q and ' JOIN ' not in q
                             for q in selects(large)), 1)
        fields = ['asset_id_snapshot', 'inventory_number', 'name', 'barcode', 'asset_type',
                  'asset_type_display', 'record_quantity_snapshot', 'purchase_value_snapshot',
                  'location_fk_id_snapshot', 'location_code', 'location_name', 'location_path', 'status_snapshot']
        for snapshot in session.snapshot_items.select_related('asset').all():
            original = _build_snapshot_item(session, snapshot.asset)
            self.assertEqual([getattr(snapshot, f) for f in fields], [getattr(original, f) for f in fields])

    def test_later_batch_failure_rolls_back_session_and_all_snapshots(self):
        self.create_assets(501)
        original = InventorySnapshotItem.objects.bulk_create
        batches = []
        def save_batch(objects, **kwargs):
            batches.append(len(objects))
            if len(batches) == 2:
                raise RuntimeError('simulated second batch failure')
            return original(objects, **kwargs)
        with patch.object(InventorySnapshotItem.objects, 'bulk_create', side_effect=save_batch):
            with self.assertRaises(RuntimeError):
                self.start()
        self.assertEqual(batches, [500, 1])
        self.assertFalse(InventorySession.objects.exists())
        self.assertFalse(InventorySnapshotItem.objects.exists())

    def test_cycle_rejected_and_ancestor_outside_scope_included(self):
        with self.assertRaises(ValidationError):
            _build_location_paths({1: ('A', 2), 2: ('B', 1)}, {1})
        self.assertEqual(_build_location_paths({1: ('A', None), 2: ('B', 1)}, {2})[2], 'A / B')


class ScanImportSecurityTests(TestCase):
    def test_no_anonymous_debug_fallback_and_session_requires_csrf(self):
        user = User.objects.create_superuser(username='import-admin')
        client = Client(enforce_csrf_checks=True)
        url = reverse('inventory:scan-file-import-api')
        with self.settings(DEBUG=True):
            self.assertIn(client.post(url, 'INV-000001\nCODE', content_type='text/plain').status_code, (302, 403))
        client.force_login(user)
        self.assertEqual(client.post(url, 'INV-000001\nCODE', content_type='text/plain').status_code, 403)
        self.assertFalse(InventoryScanBatch.objects.exists())

    def test_authenticated_import_with_csrf_still_works(self):
        user = User.objects.create_superuser(username='csrf-import-admin')
        room = Location.objects.create(name='Import room', code='CSRF-ROOM')
        Asset.objects.create(name='Item', barcode='CSRF-ITEM', location_fk=room, asset_type='fixed')
        session = start_inventory_session(created_by=user, root_locations=[room], asset_types=['fixed'])
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        token = 'a' * 32
        client.cookies['csrftoken'] = token
        response = client.post(reverse('inventory:scan-file-import-api'),
                               f'{session.number}\n{room.code}\nCSRF-ITEM',
                               content_type='text/plain', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(InventoryScanBatch.objects.get().uploaded_by, user)


class ConcurrentSnapshotStartTests(TransactionTestCase):
    def test_simultaneous_starts_have_distinct_numbers(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL concurrency test')
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        user = User.objects.create_superuser(username='concurrent-start')
        room = Location.objects.create(name='Concurrent room')
        Asset.objects.create(name='Item', barcode='CONCURRENT', asset_type='fixed', location_fk=room)
        barrier = Barrier(2)
        def start():
            try:
                worker_user = User.objects.get(pk=user.pk)
                worker_room = Location.objects.get(pk=room.pk)
                barrier.wait(timeout=10)
                return start_inventory_session(created_by=worker_user, root_locations=[worker_room],
                                               asset_types=['fixed']).number
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(sorted(results), ['INV-000001', 'INV-000002'])
        self.assertEqual(InventorySnapshotItem.objects.count(), 2)

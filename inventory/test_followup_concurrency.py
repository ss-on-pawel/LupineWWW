from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event, Barrier
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TransactionTestCase, Client
from django.db import connection, connections
from django.utils import timezone
from django.urls import reverse
from users.models import User
from locations.models import Location, OrganizationSettings
from assets.models import Asset, LtDocument
from assets.lt_documents import create_lt_document
from .models import InventoryScanTerminal, InventorySession, InventoryRawScan, InventoryObservedItem
from .services import start_inventory_session, record_terminal_raw_scan


class FollowupConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor!='postgresql':self.skipTest('PostgreSQL row locks required')
        self.user=User.objects.create_superuser(username='concurrency')
        self.room=Location.objects.create(name='Room',code='CONC-ROOM')
        self.asset=Asset.objects.create(name='Item',barcode='CONC-ITEM',asset_type='fixed',location_fk=self.room)
        self.session=start_inventory_session(created_by=self.user,root_locations=[self.room],asset_types=['fixed'])
        self.terminal=InventoryScanTerminal.objects.create(session=self.session,name='T')

    def test_close_waits_for_accepted_scan_and_rejects_later_scan(self):
        import inventory.services as services
        entered=Event();release=Event();closing=Event();original=services._record_scan_observation
        def delayed(*args,**kwargs):
            entered.set()
            if not release.wait(10):raise RuntimeError('test deadline')
            return original(*args,**kwargs)
        def scan():
            try:
                return record_terminal_raw_scan(session=self.session,terminal=self.terminal,client_scan_id='before-close',
                    code='CONC-ITEM',current_location_code='CONC-ROOM',scanned_at=timezone.now())[0].pk
            finally:connections.close_all()
        def close():
            try:
                client=Client();client.force_login(User.objects.get(pk=self.user.pk));closing.set()
                return client.post(reverse('inventory:session-close',args=[self.session.pk])).status_code
            finally:connections.close_all()
        with patch.object(services,'_record_scan_observation',side_effect=delayed),ThreadPoolExecutor(2) as pool:
            first=pool.submit(scan)
            self.assertTrue(entered.wait(10))
            last=pool.submit(close)
            try:
                self.assertTrue(closing.wait(10))
                with self.assertRaises(TimeoutError):last.result(timeout=0.1)
            finally:release.set()
            raw_id=first.result(timeout=10)
            self.assertEqual(last.result(timeout=10),302)
        self.session.refresh_from_db();self.assertEqual(self.session.status,'closed')
        self.assertEqual(InventoryRawScan.objects.get(pk=raw_id).processing_status,'processed')
        with self.assertRaises(ValueError):
            record_terminal_raw_scan(session=self.session,terminal=self.terminal,client_scan_id='after-close',
                code='CONC-ITEM',scanned_at=timezone.now())
        self.assertEqual(InventoryRawScan.objects.count(),1)

    def test_parallel_unknown_codes_have_one_observation(self):
        barrier=Barrier(2)
        def scan(i):
            try:
                barrier.wait(10)
                record_terminal_raw_scan(session=self.session,terminal=self.terminal,client_scan_id=f'unknown-{i}',
                    code='UNKNOWN-CONCURRENT',scanned_at=timezone.now())
            finally:connections.close_all()
        with ThreadPoolExecutor(2) as pool:list(pool.map(scan,range(2)))
        self.assertEqual(InventoryRawScan.objects.count(),2)
        self.assertEqual(InventoryObservedItem.objects.filter(asset__isnull=True).count(),1)

    def test_parallel_lt_issuance_has_unique_numbers(self):
        Asset.objects.filter(pk=self.asset.pk).update(status='liquidated',is_active=False)
        other=Asset.objects.create(name='Other',barcode='LT-OTHER',location_fk=self.room,status='liquidated',is_active=False)
        org=OrganizationSettings.get();barrier=Barrier(2)
        def issue(pk):
            try:
                asset=Asset.objects.get(pk=pk);barrier.wait(10)
                return create_lt_document(assets=[asset],org=org,date_of_action=timezone.localdate(),operator=self.user)[0].number
            finally:connections.close_all()
        with TemporaryDirectory() as media,self.settings(MEDIA_ROOT=media),ThreadPoolExecutor(2) as pool:
            numbers=list(pool.map(issue,[self.asset.pk,other.pk]))
        self.assertEqual(len(set(numbers)),2)
        self.assertEqual(sorted(LtDocument.objects.values_list('sequence',flat=True)),[1,2])

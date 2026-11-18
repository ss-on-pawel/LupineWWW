from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, connections, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from users.models import User
from assets.models import Asset, ScanCode
from .models import Location
from .forms import LocationForm
from .codes import next_location_code


class LocationCodeTests(TestCase):
    def test_preview_is_read_only_and_skips_all_identifier_types(self):
        Location.objects.create(name='Archived', code='LOK000001', is_active=False)
        Asset.objects.create(name='Asset code', barcode='LOK000002')
        Asset.objects.create(name='Asset number', barcode='OTHER', inventory_number='LOK000003')
        ScanCode.objects.create(code='LOK000004', asset_id=999999)
        before=Location.objects.count(), ScanCode.objects.count()
        for _ in range(2):
            form=LocationForm()
            self.assertEqual(form.fields['code'].widget.attrs['placeholder'],'LOK000005')
            self.assertFalse(form.initial.get('code'))
        self.assertEqual(before,(Location.objects.count(),ScanCode.objects.count()))

    def test_name_only_and_child_save_receive_next_available_codes(self):
        form=LocationForm({'name':'Building'})
        self.assertTrue(form.is_valid(),form.errors)
        root=form.save()
        self.assertEqual(root.code,'LOK000001')
        form=LocationForm({'name':'Office'},parent=root)
        self.assertTrue(form.is_valid(),form.errors)
        child=form.save()
        self.assertEqual(child.code,'LOK000002')
        self.assertEqual(child.parent,root)
        self.assertEqual(ScanCode.objects.get(pk=child.code).location_id,child.pk)

    def test_stale_preview_does_not_reserve_and_manual_code_is_respected(self):
        form=LocationForm({'name':'Auto'})
        self.assertEqual(form.fields['code'].widget.attrs['placeholder'],'LOK000001')
        Location.objects.create(name='Manual',code='LOK000001')
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().code,'LOK000002')
        form=LocationForm({'name':'Custom','code':'  Sala / 07  '})
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().code,'Sala / 07')

    def test_first_free_gap_is_used_and_legacy_codes_are_preserved(self):
        legacy=Location.objects.create(name='Legacy',code='LOC-000001')
        Location.objects.create(name='Later',code='LOK000003')
        self.assertEqual(Location.objects.create(name='First').code,'LOK000001')
        self.assertEqual(Location.objects.create(name='Second').code,'LOK000002')
        for data in ({'name':'Renamed'},{'name':'Renamed again','code':''}):
            form=LocationForm(data,instance=legacy)
            self.assertTrue(form.is_valid(),form.errors)
            legacy=form.save()
            self.assertEqual(legacy.code,'LOC-000001')
            self.assertNotIn('placeholder',form.fields['code'].widget.attrs)

    def test_duplicate_manual_code_and_name_fail_without_orphans(self):
        location=Location.objects.create(name='Existing',code='MANUAL')
        form=LocationForm({'name':'Other','code':'MANUAL'})
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            Location.objects.create(name='Existing')
        self.assertEqual(Location.objects.count(),1)
        self.assertFalse(ScanCode.objects.filter(code='LOK000001').exists())

    def test_manual_race_retries_and_outer_transaction_rolls_back(self):
        Location.objects.create(name='Manual',code='LOK000001')
        with patch('locations.codes.next_location_code',side_effect=['LOK000001','LOK000002']):
            self.assertEqual(Location.objects.create(name='Auto').code,'LOK000002')
        with self.assertRaises(RuntimeError), transaction.atomic():
            Location.objects.create(name='Rolled back')
            raise RuntimeError('abort')
        self.assertEqual(next_location_code(),'LOK000003')
        self.assertFalse(Location.objects.filter(name='Rolled back').exists())

    def test_view_hint_root_child_permissions_and_lite(self):
        user=User.objects.create_superuser(username='location-code-admin')
        self.client.force_login(user)
        url=reverse('locations:create')
        response=self.client.get(url)
        self.assertContains(response,'placeholder="LOK000001"')
        self.assertNotContains(response,'value="LOK000001"')
        with self.settings(LUPINE_EDITION='lite'):
            self.assertEqual(self.client.post(url,{'name':'Building'}).status_code,302)
        root=Location.objects.get(name='Building')
        child_url=reverse('locations:create-child',args=[root.pk])
        self.assertContains(self.client.get(child_url),'placeholder="LOK000002"')
        user=User.objects.create_user(username='location-code-scoped')
        user.profile.allowed_locations.add(root)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.post(child_url,{'name':'Office'}).status_code,302)

    def test_generated_code_is_recognized_by_inventory_and_labels(self):
        from inventory.services import start_inventory_session, import_inventory_scan_text
        from inventory.models import InventoryObservedItem
        from inventory.services import record_mobile_scan
        user=User.objects.create_superuser(username='location-code-inventory')
        room=Location.objects.create(name='Office')
        asset=Asset.objects.create(name='Computer',barcode='PC-AUTO-LOC',asset_type='fixed',location_fk=room)
        session=start_inventory_session(created_by=user,root_locations=[room],asset_types=['fixed'])
        batch=import_inventory_scan_text('\n'.join([session.number,room.code,asset.barcode]),uploaded_by=user)
        self.assertEqual(batch.recognized_assets_count,1)
        self.assertEqual(batch.unknown_codes_count,0)
        self.assertEqual(InventoryObservedItem.objects.get(session=session,asset=asset).scanned_location_id,room.pk)
        self.assertEqual(record_mobile_scan(session,room.code,None)['type'],'location')
        self.client.force_login(user)
        response=self.client.get(reverse('locations:labels-pdf'),{'ids':str(room.pk)})
        self.assertEqual(response.status_code,200)
        self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF'))


@skipUnless(connection.vendor=='postgresql','Requires PostgreSQL concurrency')
class LocationCodeConcurrencyTests(TransactionTestCase):
    def test_simultaneous_name_only_creates_get_distinct_sequential_codes(self):
        barrier=Barrier(3)
        def worker(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return Location.objects.create(name=f'Concurrent {index}').code
            finally:
                connections['default'].close()
        with ThreadPoolExecutor(max_workers=3) as executor:
            codes=list(executor.map(worker,range(3)))
        self.assertCountEqual(codes,['LOK000001','LOK000002','LOK000003'])
        self.assertEqual(ScanCode.objects.count(),3)

import json
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from assets.models import Asset, ScanCode
from assets.forms import AssetForm
from locations.models import Location
from locations.forms import LocationForm
from users.models import User
from inventory.models import InventoryRawScan, InventoryScanTerminal, InventoryObservedItem
from inventory.services import start_inventory_session, record_terminal_raw_scan, record_mobile_scan, import_inventory_scan_text
from inventory.views import _get_scanned_code_counts


class SharedScanCodeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Location.objects.create(name='Root', code='ROOT')
        cls.room = Location.objects.create(name='Room', code='LOK GŁ.', parent=cls.root)
        cls.other = Location.objects.create(name='Outside', code='OUTSIDE')
        cls.asset = Asset.objects.create(name='Chair', inventory_number='INV-1', barcode='THING',
                                         asset_type=Asset.AssetType.FIXED, location_fk=cls.room)
        cls.user = User.objects.create_user(username='scan-code-user')
        cls.user.profile.allowed_locations.add(cls.root)
        cls.session = start_inventory_session(created_by=cls.user, root_locations=[cls.root], asset_types=[Asset.AssetType.FIXED])
        cls.terminal = InventoryScanTerminal.objects.create(session=cls.session, name='T1', created_by=cls.user)

    def raw(self, code, scan_id='1', current=None, allowed=None):
        return record_terminal_raw_scan(session=self.session, terminal=self.terminal, client_scan_id=scan_id,
            code=code, current_location_code=current, scanned_at=timezone.now(), allowed_location_ids=allowed)[0]

    def test_forms_check_whole_database_including_archive_and_inactive_locations(self):
        self.asset.status=Asset.Status.LIQUIDATED
        self.asset.save()
        form=LocationForm({'name':'New','code':self.asset.barcode})
        self.assertFalse(form.is_valid())
        self.assertIn('code',form.errors)
        self.room.is_active=False
        self.room.save()
        form=AssetForm(instance=self.asset)
        form.cleaned_data={'barcode':self.room.code}
        with self.assertRaises(ValidationError):
            form.clean_barcode()
        form.cleaned_data={'barcode':self.asset.barcode}
        self.assertEqual(form.clean_barcode(),'THING')

    def test_database_guards_cover_bulk_create_and_queryset_updates(self):
        for operation in (
            lambda: Asset.objects.bulk_create([Asset(name='Duplicate',barcode=self.room.code)]),
            lambda: Location.objects.bulk_create([Location(name='Duplicate',code=self.asset.barcode)]),
            lambda: Asset.objects.filter(pk=self.asset.pk).update(barcode=self.room.code),
            lambda: Location.objects.filter(pk=self.room.pk).update(code=self.asset.barcode),
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                operation()
        self.asset.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(self.asset.barcode,'THING')
        self.assertEqual(self.room.code,'LOK GŁ.')

    def test_auto_location_avoids_code_used_by_asset(self):
        next_id = Location.objects.order_by('-pk').first().pk+1
        Asset.objects.create(name='Legacy code',barcode=f'LOC-{next_id:06d}')
        location=Location.objects.create(name='Automatic')
        self.assertEqual(location.code,f'LOC-{next_id:06d}-1')

    def test_significant_characters_are_not_normalized_or_renumbered(self):
        for code in ('0001','1','AbC','abc','A / # 2','__proto__','constructor','12345678-1234-1234-1234-123456789012'):
            location=Location.objects.create(name=code,code=code,parent=self.root)
            self.assertEqual(location.code,code)
            self.assertEqual(self.raw(code,scan_id=code).processing_status,InventoryRawScan.ProcessingStatus.LOCATION)

    def test_new_location_form_accepts_custom_code_and_edit_without_code_preserves_it(self):
        form=LocationForm({'name':'Imported','code':'IMP-001'},parent=self.root)
        self.assertTrue(form.is_valid(),form.errors)
        location=form.save()
        form=LocationForm({'name':'Renamed'},instance=location,parent=self.root)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.save().code,'IMP-001')

    def test_long_location_code_is_preserved_in_inventory_snapshot(self):
        from inventory.models import InventorySnapshotItem
        code='12345678-1234-1234-1234-123456789012'
        location=Location.objects.create(name='UUID',code=code,parent=self.root)
        asset=Asset.objects.create(name='UUID asset',barcode='UUID-THING',location_fk=location,asset_type=Asset.AssetType.FIXED)
        session=start_inventory_session(created_by=self.user,root_locations=[self.root],asset_types=[Asset.AssetType.FIXED])
        self.assertEqual(session.snapshot_items.get(asset=asset).location_code,code)
        self.assertEqual(InventorySnapshotItem._meta.get_field('location_code').max_length,120)

    def test_previous_code_cannot_change_meaning_for_saved_offline_scans(self):
        self.asset.barcode='NEW-THING'
        self.asset.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Location.objects.bulk_create([Location(name='Reuse',code='THING')])

    def test_explicit_deletion_releases_registration_but_archiving_does_not(self):
        disposable=Asset.objects.create(name='Disposable',barcode='FREE-AFTER-DELETE')
        disposable.delete()
        self.assertEqual(Location.objects.create(name='New owner',code='FREE-AFTER-DELETE').code,'FREE-AFTER-DELETE')
        self.asset.status=Asset.Status.LIQUIDATED
        self.asset.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Location.objects.bulk_create([Location(name='Reuse archived',code='THING')])

    def test_terminal_mobile_and_text_import_share_location_recognition(self):
        location=self.raw('LOK GŁ.')
        self.assertEqual(location.processing_result['type'],'location')
        asset=self.raw('THING','2',current='LOK GŁ.')
        self.assertEqual(asset.processing_result['type'],'asset')
        self.assertEqual(InventoryObservedItem.objects.get(asset=self.asset).scanned_location,self.room)
        self.assertEqual(record_mobile_scan(self.session,'LOK GŁ.',None)['type'],'location')
        batch=import_inventory_scan_text(f'{self.session.number}\nLOK GŁ.\nTHING',uploaded_by=self.user)
        self.assertEqual(batch.recognized_assets_count,1)
        self.assertEqual(batch.unknown_codes_count,0)
        InventoryRawScan.objects.filter(pk=location.pk).update(processing_status='processed')
        self.assertEqual(_get_scanned_code_counts(self.session),{'THING':2})

    def test_loc_prefix_does_not_turn_asset_into_location(self):
        asset=Asset.objects.create(name='Prefix asset',barcode='LOC-THING',asset_type=Asset.AssetType.FIXED,location_fk=self.room)
        raw=self.raw(asset.barcode)
        self.assertEqual(raw.processing_result['type'],'asset')
        self.assertEqual(_get_scanned_code_counts(self.session),{'LOC-THING':1})

    def test_inaccessible_inactive_and_unknown_context_never_assign_asset_to_wrong_location(self):
        allowed={self.root.pk,self.room.pk}
        raw=self.raw('THING',current=self.other.code,allowed=allowed)
        self.assertEqual(raw.processing_status,'error')
        self.assertFalse(InventoryObservedItem.objects.filter(asset=self.asset).exists())
        self.room.is_active=False
        self.room.save()
        self.assertFalse(record_mobile_scan(self.session,'THING',self.room.code)['ok'])
        self.assertFalse(record_mobile_scan(self.session,'THING','MISSING')['ok'])

    def test_historical_ambiguity_is_reported_without_switching_location(self):
        ScanCode.objects.filter(pk=self.room.code).update(asset_id=self.asset.pk)
        raw=self.raw(self.room.code)
        self.assertEqual(raw.processing_status,'error')
        self.assertIn('niejednoznaczny',raw.processing_result['error'])
        self.assertFalse(InventoryObservedItem.objects.filter(asset=self.asset).exists())
        self.asset.name='Unrelated edit'
        self.asset.save()

    def test_terminal_catalog_and_phone_token_do_not_expose_other_locations(self):
        self.client.force_login(self.user)
        response=self.client.get(reverse('inventory:terminal-session-bootstrap-api',args=[self.session.pk]))
        self.assertEqual(response.status_code,200)
        codes={row['code'] for row in response.json()['locations']}
        self.assertIn(self.room.code,codes)
        self.assertNotIn(self.other.code,codes)
        response=self.client.get(reverse('inventory:mobile-scan',args=[self.session.mobile_scan_token]))
        codes={row['code'] for row in response.context['session_data']['locations']}
        self.assertNotIn(self.other.code,codes)

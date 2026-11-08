import uuid
from datetime import date
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.urls import reverse
from assets.models import Asset, AssetWithdrawal, AssetHistoryEntry, AssetTypeDictionary
from assets.forms import AssetWithdrawalForm, AssetForm
from assets.withdrawals import withdraw_asset
from users.models import User
from locations.models import Location


class WithdrawalTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='withdraw-admin')
        self.location = Location.objects.create(name='Withdrawal room')
        self.asset = Asset.objects.create(name='Krzesła', inventory_number='W-1',
            asset_type='quantity', current_quantity=10, record_quantity=10, location_fk=self.location)
        AssetTypeDictionary.objects.filter(code='quantity').update(is_quantity_based=True)
        self.data = dict(method='sale', action_date=date(2026,11,8), notes='Sprzedano część',
            document_number='FV/1', expected_quantity=10, request_id=uuid.uuid4())
        self.client.force_login(self.user)

    def test_partial_then_full_and_history(self):
        one = withdraw_asset(asset_id=self.asset.pk, operator=self.user, data=self.data, quantity=3)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_quantity, 7)
        self.assertTrue(self.asset.is_active)
        two = withdraw_asset(asset_id=self.asset.pk, operator=self.user,
            data={**self.data, 'method':'transfer', 'expected_quantity':7, 'request_id':uuid.uuid4()})
        self.asset.refresh_from_db()
        self.assertFalse(self.asset.is_active)
        self.assertEqual(self.asset.current_quantity, 7)
        self.assertEqual(self.asset.record_quantity,10)
        self.assertEqual((one.quantity_before,one.quantity_after,two.quantity,two.quantity_after),(10,7,7,0))
        self.assertEqual(AssetHistoryEntry.objects.filter(asset=self.asset).count(),2)
        response=self.client.get(reverse('assets:detail',kwargs={'id':self.asset.pk}))
        self.assertContains(response,'Sprzedaż')
        self.assertContains(response,'Przekazanie')
        self.assertContains(response,'FV/1')

    def test_retry_does_not_subtract_again_and_changed_payload_rejected(self):
        one=withdraw_asset(asset_id=self.asset.pk,operator=self.user,data=self.data,quantity=3)
        two=withdraw_asset(asset_id=self.asset.pk,operator=self.user,data=self.data,quantity=3)
        self.assertEqual(one.pk,two.pk)
        with self.assertRaises(ValidationError):
            withdraw_asset(asset_id=self.asset.pk,operator=self.user,data={**self.data,'method':'other'},quantity=3)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.current_quantity,7)

    def test_stale_quantity_and_overwithdraw_rejected(self):
        for data,qty in [({**self.data,'expected_quantity':9},3),(self.data,11),(self.data,-1)]:
            with self.assertRaises(ValidationError):
                withdraw_asset(asset_id=self.asset.pk,operator=self.user,data=data,quantity=qty)
        self.assertFalse(AssetWithdrawal.objects.exists())

    def test_other_requires_note_and_new_unknown_forbidden(self):
        for method in ('other','unknown','bad'):
            form=AssetWithdrawalForm({**self.data,'method':method,'notes':''})
            self.assertFalse(form.is_valid())
        self.assertFalse(AssetWithdrawalForm({**self.data,'action_date':'2026-02-30'}).is_valid())

    def test_bulk_metadata_and_retry(self):
        payload={**self.data,'asset_ids':[self.asset.pk]}
        payload['action_date']='2026-11-08';payload['request_id']=str(payload['request_id'])
        url=reverse('assets:api-bulk-withdraw')
        self.assertEqual(self.client.post(url,payload,content_type='application/json').status_code,200)
        self.assertEqual(self.client.post(url,payload,content_type='application/json').status_code,200)
        self.assertEqual(AssetWithdrawal.objects.count(),1)
        self.asset.refresh_from_db();self.assertEqual(self.asset.current_quantity,10)

    def test_bulk_missing_metadata_is_rejected(self):
        response=self.client.post(reverse('assets:api-bulk-withdraw'),{'asset_ids':[self.asset.pk]},content_type='application/json')
        self.assertEqual(response.status_code,400)
        self.assertFalse(AssetWithdrawal.objects.exists())

    def test_single_endpoint_records_and_escapes_note(self):
        data={**self.data,'notes':'<script>alert(1)</script>','withdraw_quantity':'3'}
        data['request_id']=str(data['request_id'])
        self.assertEqual(self.client.post(reverse('assets:asset-withdraw',kwargs={'id':self.asset.pk}),data).status_code,302)
        response=self.client.get(reverse('assets:detail',kwargs={'id':self.asset.pk}))
        self.assertContains(response,'&lt;script&gt;')
        self.assertNotContains(response,'<script>alert(1)</script>')

    def test_ordinary_form_cannot_withdraw(self):
        self.assertNotIn('liquidated',[c[0] for c in AssetForm(instance=self.asset).fields['status'].choices])

    def test_old_inventory_cannot_overwrite_partial_withdrawal(self):
        from inventory.services import start_inventory_session
        from inventory.models import InventorySession
        session=start_inventory_session(created_by=self.user,root_locations=[self.location],asset_types=['quantity'])
        withdraw_asset(asset_id=self.asset.pk,operator=self.user,data=self.data,quantity=3)
        session.status=InventorySession.Status.CLOSED;session.save()
        self.client.post(reverse('inventory:session-apply-to-assets',kwargs={'pk':session.pk}))
        self.asset.refresh_from_db();session.refresh_from_db()
        self.assertEqual(self.asset.current_quantity,7)
        self.assertIsNone(session.applied_to_assets_at)

    def test_new_inventory_excludes_fully_withdrawn_asset(self):
        from inventory.services import start_inventory_session
        from inventory.models import InventorySnapshotItem
        withdraw_asset(asset_id=self.asset.pk,operator=self.user,data=self.data)
        session=start_inventory_session(created_by=self.user,root_locations=[self.location],asset_types=['quantity'])
        self.assertFalse(InventorySnapshotItem.objects.filter(session=session,asset=self.asset).exists())

    def test_restoration_preserves_withdrawal_history_and_quantity(self):
        withdraw_asset(asset_id=self.asset.pk,operator=self.user,data=self.data)
        self.client.post(reverse('assets:asset-restore',kwargs={'id':self.asset.pk}))
        self.asset.refresh_from_db()
        self.assertTrue(self.asset.is_active);self.assertEqual(self.asset.current_quantity,10)
        self.assertEqual(self.asset.withdrawals.count(),1)

    def test_biblioteka_backfill_requires_explicit_source_and_is_idempotent(self):
        import tempfile,json
        from django.test import override_settings
        from django.core.files.base import ContentFile
        from django.core.management import call_command
        from assets.models import AssetAttachment
        self.asset.status=Asset.Status.LIQUIDATED
        self.asset.external_id='biblioteka-grodzisk:123';self.asset.save()
        event=AssetWithdrawal.objects.create(asset=self.asset,method='unknown',is_historical=True,source_reference='migration:0035')
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            attachment=AssetAttachment(asset=self.asset,title='Oryginalna kartoteka klienta',original_filename='source-123.json',content_type='application/json',size_bytes=100,is_protected=True)
            attachment.file.save('source-123.json',ContentFile(json.dumps({'file':'likwidacjaBPGGM.xlsx','fields':{'ID':123,'Stan':'Przekazany'}}).encode()))
            call_command('backfill_withdrawal_methods',verbosity=0)
            event.refresh_from_db();self.assertEqual(event.method,'unknown')
            call_command('backfill_withdrawal_methods',apply=True,verbosity=0)
            call_command('backfill_withdrawal_methods',apply=True,verbosity=0)
            event.refresh_from_db();self.assertEqual(event.method,'transfer')
            self.assertIsNone(event.action_date);self.assertIsNone(event.quantity)

    def test_historical_unknown_has_no_fabricated_date_or_quantity(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        self.asset.status=Asset.Status.LIQUIDATED;self.asset.save()
        migration=importlib.import_module('assets.migrations.0035_historical_withdrawals')
        from types import SimpleNamespace
        migration.forward(apps,SimpleNamespace(connection=connection))
        event=self.asset.withdrawals.get()
        self.assertEqual(event.method,'unknown')
        self.assertIsNone(event.action_date);self.assertIsNone(event.quantity)

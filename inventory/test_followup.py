from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.db import connection
from django.test.utils import CaptureQueriesContext
from users.models import User
from locations.models import Location
from assets.models import Asset, AssetChangeRequest
from accounts.utils import get_accessible_location_ids
from .models import InventoryRawScan, InventoryObservedItem, InventoryScanTerminal, InventorySession
from .services import start_inventory_session, record_terminal_raw_scan
from .views import _build_inventory_session_analysis, _get_terminal_summaries, get_visible_inventory_sessions


class FollowupInventoryTests(TestCase):
    def setUp(self):
        self.a = Location.objects.create(name='A', code='ROOM-A')
        self.b = Location.objects.create(name='B', code='ROOM-B')
        self.admin = User.objects.create_superuser(username='admin')
        self.manager = User.objects.create_user(username='manager')
        self.manager.profile.role='manager'; self.manager.profile.save()
        self.user = User.objects.create_user(username='operator')
        self.user.profile.allowed_locations.add(self.a)
        self.asset = Asset.objects.create(name='Visible',barcode='VISIBLE',inventory_number='INV-ALIAS',asset_type='quantity',current_quantity=5,location_fk=self.a)
        self.hidden = Asset.objects.create(name='Hidden',barcode='HIDDEN',asset_type='fixed',location_fk=self.b)
        self.session=start_inventory_session(created_by=self.admin,root_locations=[self.a,self.b],asset_types=['quantity','fixed'])

    def test_manager_global_operator_scoped_and_no_approval(self):
        self.assertIsNone(get_accessible_location_ids(self.manager))
        self.assertEqual(get_accessible_location_ids(self.user),{self.a.pk})
        self.user.profile.asset_changes_require_approval=True;self.user.profile.save()
        self.client.force_login(self.user)
        response=self.client.post(reverse('assets:set-catalogued',args=[self.asset.pk]),{'is_catalogued':'1','expected_catalogued':'0'})
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db();self.assertTrue(self.asset.is_catalogued)
        self.assertFalse(AssetChangeRequest.objects.exists())
        self.assertEqual(self.client.get(reverse('accounts:user-list')).status_code,403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('accounts:user-list')).status_code,403)

    def test_partial_session_redacts_rows_token_and_denies_global_writes(self):
        self.client.force_login(self.user)
        response=self.client.get(reverse('inventory:session-detail',args=[self.session.pk]))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Visible')
        self.assertNotContains(response,'Hidden')
        self.assertNotContains(response,self.session.mobile_scan_token)
        self.assertEqual(response.context['snapshot_total'],1)
        self.assertEqual(self.client.post(reverse('inventory:session-close',args=[self.session.pk])).status_code,403)
        self.assertEqual(self.client.post(reverse('inventory:manual-confirmation-api',args=[self.session.pk]),
            {'asset_id':self.hidden.pk,'confirmed':True},content_type='application/json').status_code,403)
        self.session.status='closed';self.session.save()
        self.assertEqual(self.client.post(reverse('inventory:session-apply-to-assets',args=[self.session.pk])).status_code,403)
        self.hidden.refresh_from_db();self.assertEqual(self.hidden.current_quantity,1)

    def test_phone_quantity_and_duplicate_ack_use_raw_journal(self):
        url=reverse('inventory:mobile-scan-api',args=[self.session.mobile_scan_token])
        for i in range(5):
            data={'code':'VISIBLE','current_location_code':'ROOM-A','client_scan_id':f'phone-{i}'}
            first=self.client.post(url,data,content_type='application/json')
            retry=self.client.post(url,data,content_type='application/json')
            self.assertEqual(first.status_code,200)
            self.assertEqual(first.json()['raw_scan_id'],retry.json()['raw_scan_id'])
        self.assertEqual(InventoryRawScan.objects.count(),5)
        analysis=_build_inventory_session_analysis(self.session)
        item=next(i for i in analysis['inventory_work_items'] if i['snapshot'].asset_id==self.asset.pk)
        self.assertEqual(item['read_quantity'],5)
        self.assertEqual(item['row_status'],'matching')

    def test_inventory_number_is_unknown_and_does_not_count_as_barcode(self):
        url=reverse('inventory:mobile-scan-api',args=[self.session.mobile_scan_token])
        response=self.client.post(url,{'code':'INV-ALIAS','client_scan_id':'alias'},content_type='application/json')
        self.assertEqual(response.json()['type'],'unknown')
        analysis=_build_inventory_session_analysis(self.session)
        item=next(i for i in analysis['inventory_work_items'] if i['snapshot'].asset_id==self.asset.pk)
        self.assertEqual(item['read_quantity'],0)

    def test_bad_location_keeps_raw_error_without_unknown_observation(self):
        response=self.client.post(reverse('inventory:mobile-scan-api',args=[self.session.mobile_scan_token]),
            {'code':'VISIBLE','current_location_code':'MISSING','client_scan_id':'bad-context'},content_type='application/json')
        self.assertEqual(response.json()['type'],'scan_error')
        self.assertEqual(InventoryRawScan.objects.get().processing_status,'error')
        self.assertFalse(InventoryObservedItem.objects.exists())

    def test_later_quantity_edit_blocks_apply_without_partial_writes(self):
        self.session.status='closed';self.session.save()
        Asset.objects.filter(pk=self.asset.pk).update(current_quantity=7)
        self.client.force_login(self.manager)
        response=self.client.post(reverse('inventory:session-apply-to-assets',args=[self.session.pk]))
        self.assertEqual(response.status_code,302)
        self.session.refresh_from_db();self.assertIsNone(self.session.applied_to_assets_at)
        self.asset.refresh_from_db();self.assertEqual(self.asset.current_quantity,7)
        self.hidden.refresh_from_db();self.assertEqual(self.hidden.current_quantity,1)

    def test_own_scope_operator_can_close_and_apply(self):
        session=start_inventory_session(created_by=self.user,root_locations=[self.a],asset_types=['quantity'])
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse('inventory:session-close',args=[session.pk])).status_code,302)
        self.assertEqual(self.client.post(reverse('inventory:session-apply-to-assets',args=[session.pk])).status_code,302)
        session.refresh_from_db();self.assertIsNotNone(session.applied_to_assets_at)
        self.asset.refresh_from_db();self.assertEqual(self.asset.last_inventory_date,timezone.localdate())

    def test_terminal_summaries_one_query_and_visibility_does_not_scale_queries(self):
        for i in range(5): InventoryScanTerminal.objects.create(session=self.session,name=f'T{i}')
        with self.assertNumQueries(1): self.assertEqual(len(_get_terminal_summaries(self.session)),5)
        with CaptureQueriesContext(connection) as first: list(get_visible_inventory_sessions(self.user))
        for i in range(5): start_inventory_session(created_by=self.manager,root_locations=[self.a],asset_types=['quantity'])
        with CaptureQueriesContext(connection) as second: list(get_visible_inventory_sessions(self.user))
        self.assertEqual(len(first),len(second))

    def test_operator_creation_is_immediate_but_scope_is_enforced(self):
        self.user.profile.asset_changes_require_approval=True;self.user.profile.save()
        self.client.force_login(self.user)
        data={'name':'Ten new items','barcode':'NEW-TEN','current_quantity':10,'asset_type':'fixed',
              'location_fk':self.a.pk,'status':'active','technical_condition':'good','record_quantity':999}
        response=self.client.post(reverse('assets:create'),data)
        self.assertEqual(response.status_code,302)
        created=Asset.objects.get(barcode='NEW-TEN')
        self.assertEqual(created.record_quantity,10)
        self.assertEqual(created.current_quantity,10)
        self.assertFalse(AssetChangeRequest.objects.exists())
        response=self.client.post(reverse('assets:create'),dict(data,barcode='OUTSIDE',location_fk=self.b.pk))
        self.assertEqual(response.status_code,200)
        self.assertIn('location_fk',response.context['form'].errors)
        self.assertFalse(Asset.objects.filter(barcode='OUTSIDE').exists())

    def test_legacy_requests_remain_reviewable_by_global_manager(self):
        from assets.forms import AssetForm
        from assets.services import serialize_asset_form_payload,approve_asset_change_request,reject_asset_change_request
        current=serialize_asset_form_payload({name:getattr(self.hidden,name) for name in AssetForm.Meta.fields},exclude_system_managed=True)
        request=AssetChangeRequest.objects.create(requested_by=self.user,asset=self.hidden,operation='update',
            payload={'current':current,'proposed':dict(current,name='Reviewed globally')})
        approve_asset_change_request(request,self.manager)
        self.hidden.refresh_from_db();self.assertEqual(self.hidden.name,'Reviewed globally')
        another=AssetChangeRequest.objects.create(requested_by=self.user,asset=self.hidden,operation='update',payload={})
        reject_asset_change_request(another,self.manager,comment='Nieaktualny wniosek')
        another.refresh_from_db();self.assertEqual(another.status,'rejected')

    def test_direct_edit_does_not_replace_an_existing_legacy_request(self):
        from django.forms.models import model_to_dict
        request=AssetChangeRequest.objects.create(requested_by=self.admin,asset=self.asset,operation='update',
            payload={'original':'retain'})
        self.client.force_login(self.user)
        response=self.client.post(reverse('assets:update',args=[self.asset.pk]),
            dict({key: '' if value is None else value for key,value in model_to_dict(self.asset).items()},name='Immediate edit'))
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db();self.assertEqual(self.asset.name,'Immediate edit')
        request.refresh_from_db();self.assertEqual(request.payload,{'original':'retain'})
        self.assertEqual(AssetChangeRequest.objects.count(),1)
        response=self.client.post(reverse('assets:set-catalogued',args=[self.asset.pk]),
            {'is_catalogued':'1','expected_catalogued':'0'})
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db();self.assertTrue(self.asset.is_catalogued)
        request.refresh_from_db();self.assertEqual(request.payload,{'original':'retain'})

    def test_reprocess_failed_scan_after_location_fix_is_explicit(self):
        from .reprocessing import reprocess_failed_scan
        terminal=InventoryScanTerminal.objects.create(session=self.session,name='Recovery')
        raw,_=record_terminal_raw_scan(session=self.session,terminal=terminal,client_scan_id='failed',
            code='VISIBLE',current_location_code='RECOVERED',scanned_at=timezone.now())
        self.assertEqual(raw.processing_status,'error')
        Location.objects.create(name='Recovered room',code='RECOVERED')
        retry,created=record_terminal_raw_scan(session=self.session,terminal=terminal,client_scan_id='failed',
            code='VISIBLE',current_location_code='RECOVERED',scanned_at=raw.scanned_at)
        self.assertFalse(created);self.assertEqual(retry.processing_status,'error')
        recovered=reprocess_failed_scan(raw.pk,self.manager)
        self.assertEqual(recovered.processing_status,'processed')
        self.assertEqual(recovered.processing_result['reprocessing_history'][0]['operator_id'],self.manager.pk)
        self.assertEqual(InventoryRawScan.objects.count(),1)

    def test_postgresql_summary_matches_existing_analysis_for_all_scopes(self):
        if connection.vendor!='postgresql':self.skipTest('PostgreSQL aggregate implementation')
        from .sql_analysis import inventory_summary
        from .services import import_inventory_scan_text
        from .models import InventorySessionManualQuantity,InventorySessionManualConfirmation
        import_inventory_scan_text(f'\n{self.session.number}\n\tROOM-A\t\nVISIBLE\nVISIBLE\nUNKNOWN\n',uploaded_by=self.manager)
        InventorySessionManualQuantity.objects.create(session=self.session,asset=self.asset,quantity=2,updated_by=self.manager)
        InventorySessionManualConfirmation.objects.create(session=self.session,asset=self.hidden,confirmed_by=self.manager)
        self.client.post(reverse('inventory:mobile-scan-api',args=[self.session.mobile_scan_token]),
            {'code':'VISIBLE','current_location_code':'ROOM-A','client_scan_id':'sql-phone'},content_type='application/json')
        for actor in [self.manager,self.user,self.admin]:
            expected=_build_inventory_session_analysis(self.session,user=actor)
            with CaptureQueriesContext(connection) as captured:
                actual=inventory_summary(self.session,actor)
            for key,value in actual.items():self.assertEqual(value,expected[key],key)
            self.assertTrue(all(q['sql'].lstrip().upper().startswith(('SELECT','WITH')) for q in captured))

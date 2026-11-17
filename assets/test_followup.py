from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import connection
from django.apps import apps
from users.models import User
from locations.models import Location, OrganizationSettings
from inventory.services import start_inventory_session
from .models import Asset,AssetAttachment,LtDocument,LtDocumentSequence
from .lt_documents import create_lt_document


class FollowupAssetTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_superuser(username='followup-admin')
        self.root=Location.objects.create(name='Biuro <A>')
        self.child=Location.objects.create(name='Pokój',parent=self.root)
        self.asset=Asset.objects.create(name='Szafa <duża> & druga',barcode='A-1',location_fk=self.child,asset_type='fixed')
        self.client.force_login(self.user)

    def test_rename_updates_live_paths_but_not_snapshot(self):
        session=start_inventory_session(created_by=self.user,root_locations=[self.root],asset_types=['fixed'])
        before=session.snapshot_items.get().location_path
        self.root.name='Nowe biuro';self.root.save()
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.location,'Nowe biuro / Pokój')
        self.assertEqual(session.snapshot_items.get().location_path,before)
        response=self.client.get(reverse('locations:detail',args=[self.child.pk]))
        from django.utils.html import escape
        self.assertContains(response,escape(self.asset.name))

    def _archive(self,count=3):
        Asset.objects.filter(pk=self.asset.pk).update(status='liquidated',is_active=False)
        assets=[Asset.objects.get(pk=self.asset.pk)]
        for i in range(1,count):
            assets.append(Asset.objects.create(name=f'Other {i}',barcode=f'OTHER-{i}',location_fk=self.child,
                status='liquidated',is_active=False))
        return assets

    def test_lt_one_file_per_document_number_not_per_asset(self):
        assets=self._archive()
        with TemporaryDirectory() as media,self.settings(MEDIA_ROOT=media):
            for sequence in (1,2):
                document,_,pdf,_=create_lt_document(assets=assets,org=OrganizationSettings.get(),
                    date_of_action=date.today(),operator=self.user)
                self.assertEqual(document.number,f'LT/{sequence:06d}/{timezone.localdate().year}')
                self.assertEqual(document.attachments.count(),3)
                self.assertEqual(len(set(document.attachments.values_list('file',flat=True))),1)
                self.assertTrue(pdf.startswith(b'%PDF'))
            self.assertEqual(len(list(Path(media).rglob('*.pdf'))),2)

    def test_lt_failure_rolls_back_number_links_and_file(self):
        assets=self._archive()
        with TemporaryDirectory() as media,self.settings(MEDIA_ROOT=media):
            with patch.object(AssetAttachment.objects,'bulk_create',side_effect=RuntimeError('simulated failure')):
                with self.assertRaises(RuntimeError):
                    create_lt_document(assets=assets,org=OrganizationSettings.get(),date_of_action=date.today(),operator=self.user)
            self.assertFalse(LtDocument.objects.exists())
            self.assertFalse(LtDocumentSequence.objects.exists())
            self.assertEqual(list(Path(media).rglob('*.pdf')),[])

    def test_legacy_lt_numbers_seed_without_rewriting_attachments(self):
        import importlib
        Attachment=AssetAttachment
        original=Attachment.objects.create(asset=self.asset,original_filename='LT_20260102_LT-000125-2026.pdf',
            document_type=AssetAttachment.DocumentType.LT,file='old.pdf',size_bytes=1)
        module=importlib.import_module('assets.migrations.0039_seed_lt_sequences')
        from types import SimpleNamespace
        module.seed_sequences(apps,SimpleNamespace(connection=connection))
        module.seed_sequences(apps,SimpleNamespace(connection=connection))
        self.assertEqual(LtDocumentSequence.objects.get(year=2026).next_number,126)
        original.refresh_from_db();self.assertEqual(original.document_number,'')

    def test_new_quantity_and_localtime_are_consistent(self):
        from .forms import AssetForm
        form=AssetForm(data={'name':'Ten','barcode':'TEN','current_quantity':10,'asset_type':'fixed',
                            'location_fk':self.child.pk,'status':'active','technical_condition':'good'})
        self.assertTrue(form.is_valid(),form.errors)
        asset=form.save();self.assertEqual(asset.record_quantity,10)
        response=self.client.get(reverse('assets:api-list'))
        row=next(r for r in response.json()['results'] if r['id']==asset.pk)
        self.assertEqual(row['updated_at_display'],timezone.localtime(asset.updated_at).strftime('%Y-%m-%d %H:%M'))

    def test_duplicate_import_receipt_and_failed_row_rollback(self):
        from .importer import import_assets_from_rows
        from .models import AssetTypeDictionary
        kind=AssetTypeDictionary.objects.get(code='fixed')
        kind.barcode_prefix='FX';kind.save()
        row={'row_number':2,'name':'Imported','inventory_number':'00123','quantity':'3','value':'12.34','location_path':'New room'}
        before=Asset.objects.count()
        first=import_assets_from_rows([row],root_location=self.root,asset_type_ref=kind,operator=self.user,idempotency_key='a'*64)
        second=import_assets_from_rows([row],root_location=self.root,asset_type_ref=kind,operator=self.user,idempotency_key='a'*64)
        self.assertEqual(first['imported_count'],1);self.assertTrue(second['already_imported'])
        self.assertEqual(Asset.objects.count(),before+1)
        with patch('assets.importer.record_asset_history',side_effect=RuntimeError('simulated failure')):
            result=import_assets_from_rows([dict(row,location_path='Rolled back')],root_location=self.root,
                asset_type_ref=kind,operator=self.user,idempotency_key='b'*64)
        self.assertEqual(result['imported_count'],0)
        self.assertFalse(Location.objects.filter(name='Rolled back').exists())
        self.assertEqual(Asset.objects.count(),before+1)

    def test_xlsx_identifier_zero_mask_is_preserved(self):
        import io,openpyxl
        from .importer import parse_import_xlsx
        workbook=openpyxl.Workbook();sheet=workbook.active
        sheet.append(['Numer','Nazwa','Ilość','Wartość','Lokalizacja'])
        sheet.append([123,'Item',1,1,'Room']);sheet['A2'].number_format='00000'
        output=io.BytesIO();workbook.save(output);output.seek(0)
        self.assertEqual(parse_import_xlsx(output)[0]['inventory_number'],'00123')

    def test_deleting_asset_releases_only_its_historical_codes(self):
        from .models import ScanCode
        Asset.objects.filter(pk=self.asset.pk).update(barcode='RENAMED')
        old_id=self.asset.pk
        Asset.objects.filter(pk=old_id).delete()
        self.assertFalse(ScanCode.objects.filter(asset_id=old_id).exists())
        self.assertFalse(ScanCode.objects.filter(pk__in=['A-1','RENAMED']).exists())
        self.assertTrue(ScanCode.objects.filter(location_id=self.root.pk).exists())

    def test_stale_browser_form_does_not_overwrite_a_new_quantity(self):
        from django.forms.models import model_to_dict
        original=self.asset.updated_at.isoformat()
        data={key:'' if value is None else value for key,value in model_to_dict(self.asset).items()}
        data.update(name='Stale edit',expected_updated_at=original)
        Asset.objects.filter(pk=self.asset.pk).update(current_quantity=7,updated_at=timezone.now())
        response=self.client.post(reverse('assets:update',args=[self.asset.pk]),data)
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.asset.refresh_from_db();self.assertEqual(self.asset.current_quantity,7)
        self.assertNotEqual(self.asset.name,'Stale edit')

    def test_zero_quantity_can_be_preserved_during_an_edit(self):
        from .forms import AssetForm
        Asset.objects.filter(pk=self.asset.pk).update(current_quantity=0)
        self.asset.refresh_from_db()
        form=AssetForm(instance=self.asset,data={'name':'Updated zero','barcode':self.asset.barcode,
            'current_quantity':0,'asset_type':'fixed','location_fk':self.child.pk,'status':'active','technical_condition':'good'})
        self.assertTrue(form.is_valid(),form.errors)
        form.save();self.asset.refresh_from_db();self.assertEqual(self.asset.current_quantity,0)

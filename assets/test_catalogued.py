import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase, TransactionTestCase, override_settings
from django.core.files.base import ContentFile
from django.core.management import call_command, CommandError
from django.urls import reverse
from pypdf import PdfReader
from users.models import User
from locations.models import Location
from .models import Asset, AssetAttachment, AssetHistoryEntry
from .forms import AssetForm
from .catalogue_printing import generate_catalogued_pdf
from .services import capture_asset_history_values, record_asset_field_changes


class CataloguedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username='catalogue-admin', password='test')
        self.loc = Location.objects.create(name='F1', code='IMP28')
        self.asset = Asset.objects.create(name='Szafa do biurka', barcode='100407',
            location_fk=self.loc, is_catalogued=True, description='Pierwsza linia\n<script>test</script>')
        self.client.force_login(self.user)

    def test_detail_visible_and_escaped(self):
        response = self.client.get(reverse('assets:detail', args=[self.asset.pk]))
        self.assertContains(response, 'Katalogowany')
        self.assertContains(response, 'Pierwsza linia<br>')
        self.assertContains(response, '&lt;script&gt;test&lt;/script&gt;')
        self.assertNotContains(response, '<script>test</script>')

    def test_print_button_is_in_inventory_panel(self):
        url=reverse('assets:catalogued-pdf')
        self.assertContains(self.client.get(reverse('inventory:session-list')), url)
        self.assertNotContains(self.client.get(reverse('assets:list')), url)

    def test_form_checkbox_and_history(self):
        form = AssetForm(instance=self.asset)
        self.assertIn('is_catalogued', form.fields)
        before = capture_asset_history_values(self.asset)
        self.asset.is_catalogued=False
        self.asset.save()
        record_asset_field_changes(asset=self.asset, operator=self.user, before_values=before)
        entry=AssetHistoryEntry.objects.get(asset=self.asset, field_name='is_catalogued')
        self.assertEqual((entry.old_value,entry.new_value),('Tak','Nie'))

    def test_register_excludes_archive_and_unmarked_and_scopes_access(self):
        Asset.objects.create(name='Zwykły',barcode='100408',location_fk=self.loc)
        Asset.objects.create(name='Wycofany',barcode='100409',location_fk=self.loc,
                             status=Asset.Status.LIQUIDATED,is_catalogued=True)
        other=Location.objects.create(name='F2',code='IMP29')
        Asset.objects.create(name='Inny pokój',barcode='100410',location_fk=other,is_catalogued=True)
        with patch('assets.views.get_accessible_location_ids',return_value={self.loc.pk}):
            response=self.client.get(reverse('assets:catalogued-pdf'))
            self.assertEqual(response.status_code,200)
            reader=PdfReader(io.BytesIO(b''.join(response.streaming_content)))
            text=''.join(p.extract_text() for p in reader.pages)
            self.assertIn('100407',text)
            self.assertIn('IMP28',text)
            for code in ['100408','100409','100410']:
                self.assertNotIn(code,text)
        with patch('assets.views.get_accessible_location_ids',return_value=set()):
            self.assertEqual(self.client.get(reverse('assets:catalogued-pdf')).status_code,404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('assets:catalogued-pdf')).status_code,302)

    def test_pagination_and_location_repeated(self):
        rows=[self.asset]
        for i in range(7):
            rows.append(Asset.objects.create(name='Krzesło',barcode=f'20000{i}',location_fk=self.loc,is_catalogued=True))
        reader=PdfReader(generate_catalogued_pdf(rows,'Biblioteka Publiczna Gminy Grodzisk Mazowiecki'))
        self.assertEqual(len(reader.pages),2)
        for page in reader.pages:
            self.assertAlmostEqual(float(page.mediabox.width),595.2756,places=3)
            self.assertIn('IMP28',page.extract_text())
        with self.assertRaises(ValueError):
            self.asset.barcode='żółć'
            generate_catalogued_pdf([self.asset],'Biblioteka')

    def test_filter_boolean(self):
        from .filters import parse_asset_filters, apply_asset_filters
        from django.http import QueryDict
        params=QueryDict('filter__is_catalogued__equals=true')
        parsed=parse_asset_filters(params)
        self.assertFalse(parsed.errors)
        self.assertIn(self.asset,apply_asset_filters(Asset.objects.all(),parsed))
        parsed=parse_asset_filters(QueryDict('filter__is_catalogued__equals=false'))
        self.assertNotIn(self.asset,apply_asset_filters(Asset.objects.all(),parsed))

    def test_backfill_dry_run_apply_idempotence_and_error_atomicity(self):
        self.asset.is_catalogued=False
        self.asset.external_id='library:1'
        self.asset.save()
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            attachment=AssetAttachment.objects.create(asset=self.asset,title='Oryginalna kartoteka klienta',
                content_type='application/json',is_protected=True, size_bytes=28, original_filename='source.json',
                file=ContentFile(json.dumps({'fields':{'Katalog':'K'}}).encode(),name='source.json'))
            args=('backfill_catalogued_assets',)
            kwargs={'external_id_prefix':'library:', 'stdout':io.StringIO()}
            call_command(*args,**kwargs)
            self.asset.refresh_from_db()
            self.assertFalse(self.asset.is_catalogued)
            before=self.asset.description
            call_command(*args,apply=True,**kwargs)
            self.asset.refresh_from_db()
            self.assertTrue(self.asset.is_catalogued)
            self.assertEqual(self.asset.description,before)
            call_command(*args,apply=True,**kwargs)
            self.assertEqual(AssetHistoryEntry.objects.filter(asset=self.asset,field_name='is_catalogued').count(),1)
            self.asset.is_catalogued=False
            self.asset.save()
            Asset.objects.create(name='Brak źródła',barcode='80000',external_id='library:2')
            with self.assertRaises(CommandError):
                call_command(*args,apply=True,**kwargs)
            self.asset.refresh_from_db()
            self.assertFalse(self.asset.is_catalogued)


class CataloguedMigrationTests(TransactionTestCase):
    def test_forward_and_reverse_preserve_global_code_guards(self):
        from django.db import connection, transaction, IntegrityError
        from django.db.migrations.executor import MigrationExecutor
        try:
            MigrationExecutor(connection).migrate([('assets','0036_asset_identifier_requirements')])
            previous=MigrationExecutor(connection).loader.project_state([('assets','0036_asset_identifier_requirements')]).apps
            location=previous.get_model('locations','Location').objects.create(name='Migration room',code='MIG-ROOM')
            old_asset=previous.get_model('assets','Asset').objects.create(name='Original',barcode='MIG-ITEM')
            with self.assertRaises(IntegrityError), transaction.atomic():
                previous.get_model('assets','Asset').objects.filter(pk=old_asset.pk).update(barcode=location.code)
            MigrationExecutor(connection).migrate([('assets','0037_asset_is_catalogued')])
            current=Asset.objects.get(pk=old_asset.pk)
            self.assertFalse(current.is_catalogued)
            self.assertEqual(current.barcode,'MIG-ITEM')
            with self.assertRaises(IntegrityError), transaction.atomic():
                Asset.objects.filter(pk=current.pk).update(barcode=location.code)
        finally:
            MigrationExecutor(connection).migrate([('assets','0037_asset_is_catalogued')])

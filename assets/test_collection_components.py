import uuid
from django.test import TestCase
from django.urls import reverse
from users.models import User
from locations.models import Location
from .models import Asset, AssetCollection, AssetCollectionEvent, AssetCollectionMembership, ScanCode
from .collections import change_collection_members, create_collection
from .collection_components import next_component_code


class ComponentFromListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='list-set-user')
        self.room = Location.objects.create(name='Office', code='LIST-ROOM')
        self.other = Location.objects.create(name='Private', code='LIST-SECRET')
        self.user.profile.allowed_locations.add(self.room)
        self.parent = Asset.objects.create(name='Workstation', barcode='PC-001', inventory_number='INV/2026', location_fk=self.room)
        self.client.force_login(self.user)
        self.url = reverse('assets:set-component-create', args=[self.parent.pk])

    def data(self, **changes):
        data = dict(name='Monitor', asset_type='fixed', location_fk=self.room.pk,
            current_quantity=1, status='active', technical_condition='good',
            action='attach', expected_version=1, request_id=str(uuid.uuid4()))
        data.update(changes)
        return data

    def test_get_is_normal_form_and_does_not_create_an_empty_set(self):
        response = self.client.get(self.url)
        self.assertTemplateUsed(response, 'assets/asset_form.html')
        self.assertContains(response, 'PC-001/1')
        self.assertContains(response, 'INV/2026')
        self.assertContains(response, 'Eksploatacja')
        self.assertEqual(Asset.objects.count(), 1)
        self.assertFalse(AssetCollection.objects.exists())

    def test_first_save_is_atomic_inherits_numbers_and_ignores_tampering(self):
        data = self.data(inventory_number='FORGED', barcode='FORGED', current_quantity=99, purchase_value=999)
        self.assertRedirects(self.client.post(self.url, data), reverse('assets:list'))
        part = Asset.objects.get(name='Monitor')
        self.assertEqual((part.inventory_number, part.barcode, part.current_quantity, part.record_quantity), ('INV/2026', 'PC-001/1', 1, 1))
        self.assertIsNone(part.purchase_value)
        self.assertEqual(ScanCode.objects.get(pk=part.barcode).asset_id, part.pk)
        collection = AssetCollection.objects.get()
        self.assertEqual(collection.memberships.count(), 2)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(Asset.objects.count(), 2)
        self.assertEqual(AssetCollectionEvent.objects.count(), 1)
        self.assertContains(self.client.post(self.url, {**data, 'name':'Changed'}), 'innych danych')
        self.assertEqual(Asset.objects.count(), 2)

    def test_invalid_form_and_open_inventory_leave_no_orphans(self):
        self.assertEqual(self.client.post(self.url, self.data(name='')).status_code, 200)
        from inventory.services import start_inventory_session
        start_inventory_session(created_by=self.user, root_locations=[self.room], asset_types=[self.parent.asset_type])
        self.assertContains(self.client.post(self.url, self.data()), 'otwartej inwentaryzacji')
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(ScanCode.objects.filter(asset_id__isnull=False).count(), 1)
        self.assertFalse(AssetCollection.objects.exists())

    def test_second_component_and_detached_renamed_code_are_not_reused(self):
        self.client.post(self.url, self.data())
        collection = AssetCollection.objects.get()
        url = reverse('assets:collection-member-create', args=[collection.pk])
        self.assertEqual(self.client.post(url, self.data(name='Keyboard', expected_version=1)).status_code, 302)
        part = Asset.objects.get(barcode='PC-001/2')
        change_collection_members(operator=self.user, collection_id=collection.pk, action='detach',
            asset_ids=[part.pk], expected_version=2, request_id=uuid.uuid4())
        part.barcode = 'RENAMED'
        part.save()
        self.assertEqual(self.client.post(url, self.data(name='Mouse', expected_version=3)).status_code, 302)
        self.assertTrue(Asset.objects.filter(barcode='PC-001/3').exists())
        self.assertContains(self.client.post(url, self.data(name='Stale', expected_version=3)), 'Skład zmienił')
        self.assertFalse(Asset.objects.filter(name='Stale').exists())

    def test_global_identifiers_are_skipped_and_length_is_checked(self):
        Location.objects.create(name='Collision', code='PC-001/1')
        Asset.objects.create(name='Other', barcode='OTHER', inventory_number='PC-001/2', location_fk=self.room)
        self.assertEqual(next_component_code(self.parent), 'PC-001/3')
        self.parent.barcode = 'X' * 120
        self.parent.save()
        response = self.client.get(self.url)
        self.assertContains(response, 'za długi')
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 200)
        self.assertFalse(AssetCollection.objects.exists())

    def test_scope_and_archive_protected_and_metadata_hides_plus(self):
        self.assertEqual(self.client.post(self.url, self.data(location_fk=self.other.pk)).status_code, 200)
        self.client.post(self.url, self.data())
        collection = AssetCollection.objects.get()
        rows = self.client.get(reverse('assets:api-list'), {'hierarchy':1}).json()['results']
        self.assertTrue(rows[0]['collection']['can_add_component'])
        part = collection.memberships.get(role='member').asset
        part.location_fk = self.other
        part.save()
        rows = self.client.get(reverse('assets:api-list'), {'hierarchy':1}).json()['results']
        self.assertFalse(rows[0]['collection']['can_add_component'])
        self.assertEqual(self.client.get(reverse('assets:collection-member-create', args=[collection.pk])).status_code, 403)
        self.parent.location_fk = self.other
        self.parent.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_group_keeps_custom_identifiers_and_existing_creation_workflow(self):
        group = create_collection(operator=self.user, kind='group', name='Group', asset_ids=[self.parent.pk], request_id=uuid.uuid4())
        self.assertEqual(self.client.get(self.url).status_code, 404)
        response = self.client.post(reverse('assets:collection-member-create', args=[group.pk]), self.data(barcode='CUSTOM', inventory_number='OWN'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Asset.objects.filter(barcode='CUSTOM', inventory_number='OWN').exists())

    def test_lite_uses_the_same_form_without_creating_financial_records(self):
        from .models import AssetDepreciationPlan, OtDocumentNumber
        with self.settings(LUPINE_EDITION='lite'):
            self.assertEqual(self.client.get(self.url).status_code, 200)
            self.assertEqual(self.client.post(self.url, self.data()).status_code, 302)
        self.assertFalse(AssetDepreciationPlan.objects.exists())
        self.assertFalse(OtDocumentNumber.objects.exists())

    def test_suffixed_code_prints_and_confirms_only_the_physical_component(self):
        self.client.post(self.url, self.data())
        part = Asset.objects.get(barcode='PC-001/1')
        labels = self.client.get(reverse('assets:labels-pdf'), {'ids':str(part.pk)})
        self.assertEqual(labels.status_code, 200)
        self.assertTrue(b''.join(labels.streaming_content).startswith(b'%PDF'))
        from inventory.services import start_inventory_session, import_inventory_scan_text
        from inventory.views import _build_inventory_session_analysis
        session = start_inventory_session(created_by=self.user, root_locations=[self.room], asset_types=['fixed'])
        import_inventory_scan_text('\n'.join([session.number, self.room.code, part.barcode]), uploaded_by=self.user)
        analysis = _build_inventory_session_analysis(session, user=self.user)
        self.assertEqual(analysis['snapshot_total'], 1)
        self.assertEqual(analysis['matching_count'], 1)
        self.assertTrue(analysis['collection_summaries'][0]['complete'])

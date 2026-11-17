from django.test import TestCase, Client
from django.urls import reverse
from django.forms.models import model_to_dict
from users.models import User
from locations.models import Location
from .models import Asset,AssetHistoryEntry,AssetChangeRequest
from .forms import AssetForm
from .services import approve_asset_change_request


class CatalogueCardTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_superuser(username='card-admin',password='test')
        self.location=Location.objects.create(name='F1',code='CARD-ROOM')
        self.asset=Asset.objects.create(name='Szafa',barcode='CARD-ITEM',location_fk=self.location,asset_type='fixed',description='Notatka')
        self.url=reverse('assets:set-catalogued',args=[self.asset.pk])
        self.client.force_login(self.user)

    def test_card_checkbox_saves_only_one_field_and_history(self):
        before=model_to_dict(self.asset)
        response=self.client.get(reverse('assets:detail',args=[self.asset.pk]))
        self.assertContains(response,self.url)
        response=self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'})
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db()
        before['is_catalogued']=True
        self.assertEqual(model_to_dict(self.asset),before)
        entry=AssetHistoryEntry.objects.get(asset=self.asset,field_name='is_catalogued')
        self.assertEqual(entry.operator,self.user)
        self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'})
        self.assertEqual(AssetHistoryEntry.objects.filter(asset=self.asset).count(),1)
        self.client.post(self.url,{'expected_catalogued':'1'})
        self.asset.refresh_from_db();self.assertFalse(self.asset.is_catalogued)

    def test_permissions_csrf_and_conflicts(self):
        self.assertEqual(self.client.get(self.url).status_code,405)
        self.assertEqual(self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'1'}).status_code,409)
        self.assertEqual(self.client.post(self.url,{'is_catalogued':'garbage','expected_catalogued':'0'}).status_code,400)
        csrf_client=Client(enforce_csrf_checks=True);csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'}).status_code,403)
        user=User.objects.create_user(username='restricted')
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'}).status_code,404)
        self.client.force_login(self.user)
        self.asset.status=Asset.Status.LIQUIDATED;self.asset.save()
        self.assertEqual(self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'}).status_code,404)

    def test_approval_and_pending_request_not_overwritten(self):
        user=User.objects.create_user(username='approval-user')
        user.profile.asset_changes_require_approval=True
        user.profile.save(update_fields=['asset_changes_require_approval'])
        user.profile.allowed_locations.add(self.location)
        self.client.force_login(user)
        response=self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'})
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db();self.assertFalse(self.asset.is_catalogued)
        change=AssetChangeRequest.objects.get(asset=self.asset)
        self.assertEqual(self.client.post(self.url,{'is_catalogued':'1','expected_catalogued':'0'}).status_code,409)
        approve_asset_change_request(change,self.user)
        self.asset.refresh_from_db();self.assertTrue(self.asset.is_catalogued)

    def test_normal_edit_preserves_catalogued_even_if_hidden_value_is_stale(self):
        self.asset.is_catalogued=True;self.asset.save()
        data=model_to_dict(self.asset)
        data.update(is_catalogued=False,location_fk=self.location.pk)
        form=AssetForm(data=data,instance=self.asset,preserve_catalogued=True)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertTrue(form.cleaned_data['is_catalogued'])
        response=self.client.get(reverse('assets:update',args=[self.asset.pk]))
        self.assertNotContains(response,'<input type="checkbox" name="is_catalogued"')

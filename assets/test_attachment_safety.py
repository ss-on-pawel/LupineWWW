from tempfile import TemporaryDirectory
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase
from django.urls import reverse
from users.models import User
from locations.models import Location
from .models import Asset, AssetAttachment


class AttachmentSafetyTests(TransactionTestCase):
    def test_active_content_and_original_json_download_as_attachments(self):
        user = User.objects.create_superuser(username='download-admin')
        asset = Asset.objects.create(name='Safe', barcode='SAFE', location_fk=Location.objects.create(name='Room'))
        self.client.force_login(user)
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            for name, mime, content in [('payload.html', 'text/html', b'<script>alert(1)</script>'),
                                        ('original.json', 'application/json', b'{"original":true}'),
                                        ('image.svg', 'image/svg+xml', b'<svg onload="alert(1)"/>'),
                                        ('document.pdf', 'application/pdf', b'%PDF-test')]:
                response = self.client.post(reverse('assets:attachment-upload', args=[asset.pk]),
                                            {'title': name, 'file': SimpleUploadedFile(name, content, content_type=mime)})
                self.assertEqual(response.status_code, 302)
                attachment = AssetAttachment.objects.get(original_filename=name)
                response = self.client.get(reverse('assets:attachment-download', args=[asset.pk, attachment.pk]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/octet-stream')
                self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
                self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(b''.join(response.streaming_content), content)
                response.close()

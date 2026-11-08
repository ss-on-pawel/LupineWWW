import io
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from assets.forms import AssetForm
from assets.models import Asset, AssetAttachment
from locations.models import Location
from users.models import User


class OtDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.location = Location.objects.create(name="Pokój OT")
        cls.other_location = Location.objects.create(name="Inny pokój")
        cls.admin = User.objects.create_superuser(username="ot-admin", password="test-pass")
        cls.user = User.objects.create_user(username="ot-user", password="test-pass")
        cls.user.profile.allowed_locations.add(cls.location)
        cls.user.profile.asset_changes_require_approval = True
        cls.user.profile.save()
        cls.asset = Asset.objects.create(
            name="Łóżko <model> & wyposażenie", inventory_number="",
            barcode="OT-TEST-1", location_fk=cls.location, external_id="ot-source:1",
            ot_document_number="OT/12/2020", invoice_number="FV/1/2020",
            commissioning_date=date(2020, 2, 3), record_quantity=4, current_quantity=2,
            purchase_value=0,
        )

    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings = override_settings(MEDIA_ROOT=self.media.name)
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        self.client.force_login(self.admin)
        self.url = reverse("assets:ot-generate", args=[self.asset.pk])

    def payload(self, **kwargs):
        return {"document_number": "OT/12/2020", "expected_document_number": "OT/12/2020",
                "expected_commissioning_date": "2020-02-03",
                "date_of_action": "2020-02-03", "notes": "Podstawa <umowa> & odbiór", **kwargs}

    def generate(self, **kwargs):
        response = self.client.post(self.url, self.payload(**kwargs))
        if response.streaming:
            self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
            response.close()
        return response

    def test_get_prefills_existing_number_and_commissioning_date_without_writes(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["document_number"], "OT/12/2020")
        self.assertEqual(response.context["form"].initial["date_of_action"], date(2020, 2, 3))
        self.assertContains(response, 'name="date_of_action" value="2020-02-03"')
        self.assertFalse(self.asset.attachments.exists())

    def test_number_field_is_optional_and_separate_from_invoice(self):
        field = AssetForm().fields["ot_document_number"]
        self.assertFalse(field.required)
        self.assertEqual(field.label, "Dokument OT")
        self.assertTrue(AssetForm().fields["inventory_number"].required)

    def test_generation_saves_commissioning_date_preserving_other_asset_fields(self):
        before = Asset.objects.values().get(pk=self.asset.pk)
        self.assertEqual(self.generate(date_of_action="2019-01-02").status_code, 200)
        after = Asset.objects.values().get(pk=self.asset.pk)
        self.assertEqual(after["commissioning_date"], date(2019, 1, 2))
        for key in ("commissioning_date", "updated_at"):
            before.pop(key)
            after.pop(key)
        self.assertEqual(before, after)
        self.assertTrue(self.asset.history_entries.filter(field_name="commissioning_date",
                         old_value="2020-02-03", new_value="2019-01-02").exists())
        attachment = self.asset.attachments.get()
        self.assertEqual(attachment.document_type, "ot")
        self.assertEqual(attachment.document_number, "OT/12/2020")
        self.assertEqual(attachment.date_of_action, date(2019, 1, 2))
        self.assertTrue(attachment.is_protected and attachment.is_system_generated)
        self.assertTrue(self.asset.history_entries.filter(description="Wygenerowano dokument OT").exists())

    def test_missing_date_is_saved_and_restored_with_number_when_reopening_form(self):
        Asset.objects.filter(pk=self.asset.pk).update(commissioning_date=None)
        for _ in range(2):
            response = self.generate(document_number="OT/NEW/2026", date_of_action="2026-11-08",
                                     expected_commissioning_date="")
            self.assertEqual(response["Content-Type"], "application/pdf")
        response = self.client.get(self.url)
        self.assertEqual(response.context["form"].initial["document_number"], "OT/NEW/2026")
        self.assertEqual(response.context["form"].initial["date_of_action"], date(2026, 11, 8))
        response = self.client.get(reverse("assets:api-list"))
        self.assertEqual(response.json()["results"][0]["commissioning_date"], "2026-11-08")
        self.assertEqual(self.asset.history_entries.filter(field_name="commissioning_date").count(), 1)

    def test_other_tab_date_change_is_protected(self):
        Asset.objects.filter(pk=self.asset.pk).update(commissioning_date=date(2021, 1, 1))
        response = self.generate()
        self.assertContains(response, "Data przyjęcia zmieniła się")
        self.assertFalse(self.asset.attachments.exists())
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.commissioning_date, date(2021, 1, 1))

    def test_date_cannot_bypass_approval_and_is_not_saved_on_pdf_failure(self):
        self.client.force_login(self.user)
        response = self.generate(date_of_action="2026-11-08")
        self.assertContains(response, "Zmiana daty wymaga zatwierdzenia")
        self.assertFalse(self.asset.attachments.exists())
        self.client.force_login(self.admin)
        with patch("assets.documents.generate_ot_pdf", side_effect=RuntimeError("PDF failure")):
            with self.assertRaises(RuntimeError):
                self.generate(date_of_action="2026-11-08", document_number="NEW")
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.commissioning_date, date(2020, 2, 3))
        self.assertEqual(self.asset.ot_document_number, "OT/12/2020")

    def test_admin_can_set_number_without_filling_blank_inventory_number(self):
        self.assertEqual(self.generate(document_number="OT/15/2020").status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "OT/15/2020")
        self.assertEqual(self.asset.inventory_number, "")
        self.assertEqual(self.asset.invoice_number, "FV/1/2020")
        self.assertTrue(self.asset.history_entries.filter(field_name="ot_document_number",
                                                        old_value="OT/12/2020", new_value="OT/15/2020").exists())

    def test_existing_source_number_is_preserved_exactly(self):
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="  OT history  ")
        response = self.generate(document_number="  OT history  ", expected_document_number="  OT history  ")
        self.assertEqual(response.status_code, 200)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "  OT history  ")

    def test_repeated_generation_keeps_earlier_pdf_and_number(self):
        self.generate()
        first = self.asset.attachments.get()
        original = first.file.read()
        first.file.close()
        self.generate(document_number="OT/NEW")
        first.refresh_from_db()
        self.assertEqual(first.document_number, "OT/12/2020")
        self.assertEqual(first.file.read(), original)
        first.file.close()
        self.assertEqual(self.asset.attachments.count(), 2)

    def test_archived_asset_can_reprint_historical_ot(self):
        Asset.objects.filter(pk=self.asset.pk).update(status=Asset.Status.LIQUIDATED, is_active=False)
        self.assertEqual(self.generate().status_code, 200)
        self.asset.refresh_from_db()
        self.assertFalse(self.asset.is_active)

    def test_invalid_form_does_not_write(self):
        for kwargs in ({"date_of_action": ""}, {"date_of_action": "not-date"},
                       {"document_number": "X" * 121},
                       {"notes": "X" * 1001}):
            response = self.generate(**kwargs)
            self.assertTrue(response.context["form"].errors)
        self.assertFalse(self.asset.attachments.exists())

    def test_stale_form_cannot_replace_number(self):
        response = self.generate(expected_document_number="old-value", document_number="DIFFERENT")
        self.assertContains(response, "Numer OT zmienił się")
        self.assertFalse(self.asset.attachments.exists())

    def test_same_form_can_download_again_after_saving_new_number(self):
        payload = self.payload(document_number="OT/NEW")
        for _ in range(2):
            response = self.client.post(self.url, payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF-"))
            response.close()
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "OT/NEW")
        self.assertEqual(self.asset.attachments.count(), 2)
        self.assertEqual(self.asset.history_entries.filter(field_name="ot_document_number").count(), 1)

    def test_same_form_still_cannot_overwrite_number_changed_in_another_tab(self):
        self.generate(document_number="OT/FIRST")
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="OT/OTHER-TAB")
        response = self.generate(document_number="OT/FIRST")
        self.assertContains(response, "Numer OT zmienił się")
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "OT/OTHER-TAB")
        self.assertEqual(self.asset.attachments.count(), 1)

    def test_login_scope_lite_and_csrf_are_enforced(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Asset.objects.filter(pk=self.asset.pk).update(location_fk=self.other_location)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.generate().status_code, 404)
        self.client.force_login(self.admin)
        with override_settings(LUPINE_EDITION="lite"):
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.generate().status_code, 403)
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url, self.payload()).status_code, 403)

    def test_user_can_generate_stored_number_but_cannot_bypass_approval(self):
        self.client.force_login(self.user)
        self.assertEqual(self.generate().status_code, 200)
        response = self.generate(document_number="CHANGED")
        self.assertContains(response, "wymaga zatwierdzenia")
        self.assertEqual(self.asset.attachments.count(), 1)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "OT/12/2020")

    def test_protected_ot_cannot_be_deleted_even_by_admin(self):
        self.generate()
        attachment = self.asset.attachments.get()
        response = self.client.post(reverse("assets:attachment-delete", args=[self.asset.pk, attachment.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(AssetAttachment.objects.filter(pk=attachment.pk).exists())

    def test_history_failure_rolls_back_number_attachment_and_new_file(self):
        with patch("assets.views.record_asset_history", side_effect=RuntimeError("write failure")):
            with self.assertRaises(RuntimeError):
                self.generate(document_number="CHANGED")
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "OT/12/2020")
        self.assertFalse(self.asset.attachments.exists())
        self.assertEqual(list(Path(self.media.name).rglob("*.pdf")), [])

    def test_api_export_filter_and_detail_expose_ot(self):
        response = self.client.get(reverse("assets:api-list"))
        self.assertEqual(response.json()["results"][0]["ot_document_number"], "OT/12/2020")
        response = self.client.get(reverse("assets:api-export"), {"columns": "ot_document_number,invoice_number"})
        self.assertContains(response, "OT/12/2020")

    def test_ot_filter_and_sort_work_on_list_api(self):
        Asset.objects.create(name="Second", inventory_number="OT-SECOND", ot_document_number="ZZ",
                             location_fk=self.location)
        response = self.client.get(reverse("assets:api-list"), {"filter__ot_document_number__equals": "OT/12/2020"})
        self.assertEqual([row["id"] for row in response.json()["results"]], [self.asset.pk])
        response = self.client.get(reverse("assets:api-list"), {"ordering": "-ot_document_number"})
        self.assertEqual(response.json()["results"][0]["ot_document_number"], "ZZ")

    def test_pre_ot_pending_request_can_be_approved_and_preserves_current_ot(self):
        from .models import AssetChangeRequest
        from .services import approve_asset_change_request, serialize_asset_form_payload
        self.asset.inventory_number = "OT-LEGACY"
        self.asset.save()
        self.asset.refresh_from_db()
        current = serialize_asset_form_payload(
            {name: getattr(self.asset, name) for name in AssetForm.Meta.fields}, exclude_system_managed=True,
        )
        current.pop("ot_document_number")
        proposed = {**current, "name": "New name"}
        request = AssetChangeRequest.objects.create(
            requested_by=self.user, operation="update", asset=self.asset,
            payload={"current": current, "proposed": proposed},
        )
        approve_asset_change_request(request, self.admin)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.name, "New name")
        self.assertEqual(self.asset.ot_document_number, "OT/12/2020")
        response = self.client.get(reverse("assets:detail", args=[self.asset.pk]))
        self.assertContains(response, self.url)
        self.assertContains(response, "OT/12/2020")

    def source(self, number, asset=None):
        return AssetAttachment.objects.create(
            asset=asset or self.asset, title="Oryginalna kartoteka klienta", is_protected=True,
            content_type="application/json", size_bytes=1, original_filename="source.json",
            file=ContentFile(json.dumps({"fields": {"Dokument OT": number}}).encode(), name="source.json"),
        )

    def backfill(self, apply=False):
        args = ["--external-id-prefix", "ot-source:"] + (["--apply"] if apply else [])
        call_command("backfill_ot_documents", *args, stdout=io.StringIO(), stderr=io.StringIO())

    def test_backfill_dry_run_apply_and_idempotence_preserve_sources(self):
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="")
        source = self.source("  OT/OLD/1  ")
        original = source.file.read()
        source.file.close()
        self.backfill()
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "")
        self.backfill(apply=True)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "  OT/OLD/1  ")
        self.backfill(apply=True)
        self.assertEqual(self.asset.history_entries.filter(field_name="ot_document_number").count(), 1)
        source.refresh_from_db()
        self.assertEqual(source.file.read(), original)
        source.file.close()
        self.assertEqual(self.asset.invoice_number, "FV/1/2020")

    def test_backfill_conflict_aborts_entire_batch(self):
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="")
        self.source("OT/OLD")
        other = Asset.objects.create(name="Other", external_id="ot-source:2", ot_document_number="OT/EXISTING")
        self.source("OT/DIFFERENT", other)
        with self.assertRaises(CommandError):
            self.backfill(apply=True)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number, "")

    def test_backfill_ambiguous_or_invalid_source_does_not_write(self):
        self.source("OT/A")
        self.source("OT/B")
        with self.assertRaises(CommandError):
            self.backfill(apply=True)

    def test_pdf_content_and_many_pages(self):
        from types import SimpleNamespace
        from django.utils import timezone
        from .documents import generate_ot_pdf
        result = generate_ot_pdf(
            assets=[self.asset], org=SimpleNamespace(full_name="Biblioteka & szkoła <filia>", short_name="Łódź", report_footer=""),
            date_of_action=date(2020, 2, 3), generated_by=self.admin, generated_at=timezone.now(),
            doc_number="OT/<12>&2020", notes="A & B <uwagi> " * 70,
        )
        self.assertTrue(result.getvalue().startswith(b"%PDF-"))

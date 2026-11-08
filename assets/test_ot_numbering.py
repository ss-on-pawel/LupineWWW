from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import Asset, AssetAttachment, OtDocumentNumber, OtDocumentSequence
from .ot_numbering import allocate_ot_number, reserve_ot_number, suggest_ot_number
from . import test_ot_documents as workflow


class OtNumberingServiceTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(name="One", inventory_number="1")
        self.other = Asset.objects.create(name="Two", inventory_number="2")

    def test_suggestion_does_not_reserve_or_create_counter(self):
        self.assertEqual(suggest_ot_number(2026), "OT/001/2026")
        self.assertFalse(OtDocumentSequence.objects.exists())
        self.assertFalse(OtDocumentNumber.objects.exists())

    def test_sequential_numbers_and_year_reset(self):
        self.assertEqual(allocate_ot_number(self.asset.pk, 2026), "OT/001/2026")
        self.assertEqual(allocate_ot_number(self.other.pk, 2026), "OT/002/2026")
        self.assertEqual(allocate_ot_number(self.other.pk, 2027), "OT/001/2027")

    def test_existing_numbers_and_archived_attachments_are_not_reused(self):
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="OT/007/2026")
        AssetAttachment.objects.create(asset=self.asset, document_type="ot", document_number="OT/011/2026",
            title="Old", original_filename="old.pdf", content_type="application/pdf", size_bytes=0)
        self.assertEqual(allocate_ot_number(self.other.pk, 2026), "OT/012/2026")

    def test_duplicates_and_padding_variants_rejected_and_reservation_survives_number_change(self):
        reserve_ot_number("OT/001/2026", self.asset.pk)
        for number in ("OT/001/2026", "ot/1/2026", "  OT/0001/2026  "):
            with self.assertRaises(ValidationError):
                reserve_ot_number(number, self.other.pk)
        self.other.ot_document_number="OT/001/2026"
        with self.assertRaises(ValidationError):
            self.other.save()
        self.other.refresh_from_db()
        self.assertEqual(self.other.ot_document_number, "")
        self.assertEqual(suggest_ot_number(2026), "OT/002/2026")

    def test_historical_duplicate_can_reprint_but_no_new_asset_can_claim_it(self):
        Asset.objects.filter(pk__in=[self.asset.pk,self.other.pk]).update(ot_document_number="OLD-SHARED")
        reserve_ot_number("OLD-SHARED",self.asset.pk,allow_existing=True)
        reserve_ot_number("OLD-SHARED",self.other.pk,allow_existing=True)
        third=Asset.objects.create(name="Three")
        with self.assertRaises(ValidationError):
            reserve_ot_number("OLD-SHARED",third.pk)

    def test_migration_reserves_existing_shared_and_attachment_numbers_without_changing_assets(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        Asset.objects.filter(pk__in=[self.asset.pk,self.other.pk]).update(ot_document_number="OT/001/2026")
        AssetAttachment.objects.create(asset=self.asset, document_type="ot", document_number="OT/009/2026",
            title="Old", original_filename="old.pdf", content_type="application/pdf", size_bytes=0)
        migration=importlib.import_module('assets.migrations.0030_ot_numbering')
        migration.reserve_existing_numbers(apps,SimpleNamespace(connection=connection))
        self.assertEqual(OtDocumentNumber.objects.count(),2)
        self.assertEqual(Asset.objects.filter(ot_document_number="OT/001/2026").count(),2)
        self.assertEqual(suggest_ot_number(2026),"OT/010/2026")


class OtNumberingViewTests(TestCase):
    setUpTestData = classmethod(workflow.OtDocumentTests.setUpTestData.__func__)
    setUp = workflow.OtDocumentTests.setUp
    payload = workflow.OtDocumentTests.payload
    generate = workflow.OtDocumentTests.generate
    def test_placeholder_and_automatic_number_repeated_blank_post(self):
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="")
        with patch("assets.ot_numbering.timezone.localdate", return_value=date(2026,11,8)):
            response=self.client.get(self.url)
            self.assertContains(response, 'placeholder="OT/001/2026"')
            self.assertNotContains(response, 'Przykład:')
            self.assertFalse(OtDocumentSequence.objects.exists())
            for _ in range(2):
                response=self.generate(document_number="",expected_document_number="")
                self.assertEqual(response['Content-Type'],'application/pdf')
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.ot_document_number,"OT/001/2026")
        self.assertEqual(self.asset.attachments.count(),2)
        self.assertEqual(OtDocumentSequence.objects.get(year=2026).next_number,2)

    def test_manual_duplicate_is_form_error_without_new_pdf(self):
        Asset.objects.create(name="Other",ot_document_number="OT/001/2026")
        response=self.generate(document_number="OT/1/2026")
        self.assertContains(response,"Ten numer OT jest już wykorzystany")
        self.assertFalse(self.asset.attachments.exists())

    def test_failed_pdf_releases_automatic_number(self):
        Asset.objects.filter(pk=self.asset.pk).update(ot_document_number="")
        before=OtDocumentNumber.objects.count()
        with patch("assets.documents.generate_ot_pdf",side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.generate(document_number="",expected_document_number="")
        self.assertFalse(OtDocumentSequence.objects.exists())
        self.assertEqual(OtDocumentNumber.objects.count(),before)

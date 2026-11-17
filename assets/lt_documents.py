"""Transactional LT numbering and one protected file shared by asset links."""
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from .models import Asset, AssetAttachment, LtDocument, LtDocumentSequence


def create_lt_document(*, assets, org, date_of_action, operator, issuing_unit_text='', notes=''):
    from .documents import generate_lt_pdf
    saved_name = None
    storage = LtDocument._meta.get_field('file').storage
    try:
        with transaction.atomic():
            ids = {asset.pk for asset in assets}
            locked = list(Asset.objects.select_for_update(of=('self',)).select_related(
                'asset_type_ref', 'location_fk').filter(pk__in=ids, is_active=False).order_by('pk'))
            if len(locked) != len(ids) or not locked:
                raise ValidationError('Stan środków zmienił się. Odśwież formularz LT.')
            now = timezone.now()
            year = timezone.localdate(now).year
            LtDocumentSequence.objects.get_or_create(year=year)
            counter = LtDocumentSequence.objects.select_for_update().get(year=year)
            sequence = counter.next_number
            counter.next_number += 1
            counter.save(update_fields=['next_number'])
            number = f'LT/{sequence:06d}/{year}'
            document = LtDocument.objects.create(number=number, year=year, sequence=sequence,
                date_of_action=date_of_action, issued_by=operator, asset_ids=sorted(ids))
            pdf = generate_lt_pdf(assets=locked, org=org, date_of_action=date_of_action,
                generated_by=operator, generated_at=now, issuing_unit_text=issuing_unit_text,
                notes=notes, doc_number=number).getvalue()
            filename = f'LT_{date_of_action:%Y%m%d}_{number.replace("/", "-")}.pdf'
            document.file.save(filename, ContentFile(pdf), save=False)
            saved_name = document.file.name
            document.save(update_fields=['file'])
            links = [AssetAttachment(asset=asset, file=saved_name,
                title=f'LT — Likwidacja ({date_of_action:%d.%m.%Y})', original_filename=filename,
                content_type='application/pdf', size_bytes=len(pdf), uploaded_by=operator,
                document_type=AssetAttachment.DocumentType.LT, document_number=number,
                date_of_action=date_of_action, is_system_generated=True, is_protected=True,
                lt_document=document) for asset in locked]
            AssetAttachment.objects.bulk_create(links, batch_size=200)
        return document, links[0], pdf, filename
    except Exception:
        # File storage is not rolled back by the database. Only remove this
        # attempt's UUID path; never touch an existing document or old attachment.
        if saved_name:
            storage.delete(saved_name)
        raise

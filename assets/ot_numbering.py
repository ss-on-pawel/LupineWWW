"""Per-database OT numbering. GET suggests; atomic reservation allocates."""
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone


def number_key(number):
    text = number.strip().casefold()
    match = re.fullmatch(r"ot/(\d+)/(\d{4})", text)
    return f"ot/{int(match[1])}/{match[2]}" if match else text


def _used_numbers():
    from .models import Asset, AssetAttachment, OtDocumentNumber
    yield from Asset.objects.exclude(ot_document_number="").values_list("ot_document_number", flat=True).iterator()
    yield from AssetAttachment.objects.filter(document_type="ot").exclude(document_number="").values_list("document_number", flat=True).iterator()
    yield from OtDocumentNumber.objects.values_list("key", flat=True).iterator()


def suggest_ot_number(year=None):
    from .models import OtDocumentSequence
    year = year or timezone.localdate().year
    next_number = OtDocumentSequence.objects.filter(year=year).values_list("next_number", flat=True).first() or 1
    for number in _used_numbers():
        match = re.fullmatch(r"ot/(\d+)/(\d{4})", number_key(number))
        if match and int(match[2]) == year:
            next_number = max(next_number, int(match[1]) + 1)
    return f"OT/{next_number:03d}/{year}"


def check_ot_number_available(number, asset_id=None, allow_existing=False):
    from .models import Asset, AssetAttachment, OtDocumentNumber
    key = number_key(number)
    if not key:
        return
    # Existing shared historical numbers may be reprinted; never reassigned.
    if allow_existing and asset_id and Asset.objects.filter(pk=asset_id, ot_document_number=number).exists():
        return
    reservation = OtDocumentNumber.objects.filter(key=key).first()
    if reservation and reservation.asset_id != asset_id:
        raise _conflict()
    for other_id, other_number in Asset.objects.exclude(ot_document_number="").exclude(pk=asset_id).values_list("pk", "ot_document_number").iterator():
        if number_key(other_number) == key:
            raise _conflict()
    for other_id, other_number in AssetAttachment.objects.filter(document_type="ot").exclude(document_number="").exclude(asset_id=asset_id).values_list("asset_id", "document_number").iterator():
        if number_key(other_number) == key:
            raise _conflict()


def _conflict():
    return ValidationError({"ot_document_number": "Ten numer OT jest już wykorzystany w tej bazie. Wybierz inny numer lub pozostaw pole puste, aby AMS nadał kolejny."})


@transaction.atomic
def reserve_ot_number(number, asset_id, allow_existing=False):
    from .models import OtDocumentNumber
    check_ot_number_available(number, asset_id, allow_existing)
    row, _ = OtDocumentNumber.objects.get_or_create(key=number_key(number), defaults={"asset_id": asset_id})
    if row.asset_id != asset_id and not allow_existing:
        raise _conflict()


@transaction.atomic
def allocate_ot_number(asset_id, year=None):
    from .models import OtDocumentSequence
    year = year or timezone.localdate().year
    sequence, _ = OtDocumentSequence.objects.select_for_update().get_or_create(year=year)
    # Lock an existing sequence too when get_or_create encountered a concurrent insert.
    sequence = OtDocumentSequence.objects.select_for_update().get(pk=sequence.pk)
    while True:
        number = suggest_ot_number(year)
        sequence.next_number = int(number.split("/")[1]) + 1
        sequence.save(update_fields=["next_number"])
        try:
            reserve_ot_number(number, asset_id)
        except ValidationError:
            continue
        return number

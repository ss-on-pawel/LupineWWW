"""Read-only code suggestions and atomic allocation for newly created locations."""
from django.core.exceptions import ValidationError
from django.db import connection, transaction, IntegrityError


def next_location_code():
    """Return the first free LOK number without reserving or saving anything."""
    from .models import Location
    from assets.models import Asset, ScanCode
    occupied = set()
    for query in (
        Location.objects.filter(code__startswith='LOK').values_list('code', flat=True),
        ScanCode.objects.filter(code__startswith='LOK').values_list('code', flat=True),
        Asset.objects.filter(barcode__startswith='LOK').values_list('barcode', flat=True),
        Asset.objects.filter(inventory_number__startswith='LOK').values_list('inventory_number', flat=True),
    ):
        occupied.update(query.iterator())
    number = 1
    while f'LOK{number:06d}' in occupied:
        number += 1
    return f'LOK{number:06d}'


def _code_conflict(error):
    from assets.scan_codes import is_scan_code_conflict
    constraint = getattr(getattr(error.__cause__, 'diag', None), 'constraint_name', '') or ''
    return (is_scan_code_conflict(error) or constraint.startswith('locations_location_code_')
        or 'UNIQUE constraint failed: locations_location.code' in str(error))


@transaction.atomic
def save_with_automatic_code(location, args, kwargs):
    # Dedicated namespace, separate from inventory numbering. Concurrent automatic
    # writers serialize even when the locations table is empty. Manual-code races
    # still use the shared ScanCode database constraint and a fresh savepoint.
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(1280265044, 1)')
    for attempt in range(20):
        location.code = next_location_code()
        try:
            with transaction.atomic():
                # The nonempty code uses the ordinary validation/save path.
                return location.save(*args, **kwargs)
        except ValidationError as error:
            if not hasattr(error, 'message_dict') or set(error.message_dict) != {'code'}:
                raise
        except IntegrityError as error:
            if not _code_conflict(error):
                raise
        location.pk = None
        location._state.adding = True
    location.code = ''
    raise ValidationError({'code': 'Nie udało się nadać wolnego kodu. Spróbuj zapisać lokalizację ponownie.'})

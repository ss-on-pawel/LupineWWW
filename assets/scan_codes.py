from django.core.exceptions import ValidationError


MESSAGE = "Kod jest już używany przez środek lub lokalizację w tej bazie."


def validate_scan_code(code, *, kind, owner_id=None):
    if not code:
        return
    from .models import Asset, ScanCode
    from locations.models import Location
    field = 'barcode' if kind == 'asset' else 'code'
    current = (Asset if kind == 'asset' else Location).objects.filter(pk=owner_id).values_list(field, flat=True).first() if owner_id else None
    if owner_id and current == code:
        return  # Preserve unchanged historical identifiers, including reported collisions.
    row = ScanCode.objects.filter(pk=code).first()
    if row and (getattr(row, kind+'_id') != owner_id or getattr(row, ('location' if kind=='asset' else 'asset')+'_id') is not None):
        raise ValidationError(MESSAGE)
    assets = Asset.objects.filter(barcode=code)
    locations = Location.objects.filter(code=code)
    if kind == 'asset':
        assets = assets.exclude(pk=owner_id)
    else:
        locations = locations.exclude(pk=owner_id)
    if assets.exists() or locations.exists():
        raise ValidationError(MESSAGE)


def is_scan_code_conflict(error):
    cause = getattr(error, '__cause__', None)
    constraint = getattr(getattr(cause, 'diag', None), 'constraint_name', None)
    return constraint == 'scan_code_global_unique' or 'scan_code_global_unique' in str(error)

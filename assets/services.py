from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone


def _format_empty(value):
    return "" if value is None else str(value)


def _format_bool(value):
    if value is None:
        return ""
    return "Tak" if value else "Nie"


def _format_date(value):
    if value is None:
        return ""
    return value.isoformat()


def _format_datetime(value):
    if value is None:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")


def _format_decimal(value):
    return "" if value is None else str(value)


def _format_person(value):
    if value is None:
        return ""
    full_name = value.get_full_name().strip()
    return full_name or value.get_username()


def _format_location(value):
    return "" if value is None else value.path


def _format_asset_type(asset):
    if asset.asset_type_ref_id and asset.asset_type_ref:
        return asset.asset_type_ref.name
    return asset.get_asset_type_display() or asset.asset_type


def _format_choice(asset, field_name):
    value = getattr(asset, field_name)
    if value in (None, ""):
        return ""
    return getattr(asset, f"get_{field_name}_display")()


ASSET_HISTORY_FIELD_SPECS = {
    "inventory_number": {
        "label": "Numer inwentarzowy",
        "description": "Zmieniono numer inwentarzowy",
        "value": lambda asset: _format_empty(asset.inventory_number),
    },
    "name": {
        "label": "Nazwa",
        "description": "Zmieniono nazwę",
        "value": lambda asset: _format_empty(asset.name),
    },
    "asset_type": {
        "label": "Rodzaj",
        "description": "Zmieniono rodzaj",
        "value": _format_asset_type,
    },
    "status": {
        "label": "Status",
        "description": "Zmieniono status",
        "value": lambda asset: _format_choice(asset, "status"),
    },
    "is_active": {
        "label": "Aktywny",
        "description": "Zmieniono aktywność",
        "value": lambda asset: _format_bool(asset.is_active),
    },
    "location_fk": {
        "label": "Lokalizacja",
        "description": "Zmieniono lokalizację",
        "value": lambda asset: _format_location(asset.location_fk),
    },
    "responsible_person": {
        "label": "Osoba odpowiedzialna",
        "description": "Zmieniono osobę odpowiedzialną",
        "value": lambda asset: _format_person(asset.responsible_person),
    },
    "current_user": {
        "label": "Użytkownik",
        "description": "Zmieniono użytkownika",
        "value": lambda asset: _format_person(asset.current_user),
    },
    "technical_condition": {
        "label": "Stan techniczny",
        "description": "Zmieniono stan techniczny",
        "value": lambda asset: _format_choice(asset, "technical_condition"),
    },
    "purchase_value": {
        "label": "Wartość",
        "description": "Zmieniono wartość",
        "value": lambda asset: _format_decimal(asset.purchase_value),
    },
    "barcode": {
        "label": "Kod kreskowy",
        "description": "Zmieniono kod kreskowy",
        "value": lambda asset: _format_empty(asset.barcode),
    },
    "serial_number": {
        "label": "Numer seryjny",
        "description": "Zmieniono numer seryjny",
        "value": lambda asset: _format_empty(asset.serial_number),
    },
    "department": {
        "label": "Dział",
        "description": "Zmieniono dział",
        "value": lambda asset: _format_empty(asset.department),
    },
    "organizational_unit": {
        "label": "Jednostka organizacyjna",
        "description": "Zmieniono jednostkę organizacyjną",
        "value": lambda asset: _format_empty(asset.organizational_unit),
    },
    "room": {
        "label": "Pomieszczenie",
        "description": "Zmieniono pomieszczenie",
        "value": lambda asset: _format_empty(asset.room),
    },
}


def record_asset_history(
    *,
    asset,
    operator=None,
    event_type,
    description,
    old_value="",
    new_value="",
    field_name="",
    source_object=None,
    occurred_at=None,
):
    from .models import AssetHistoryEntry

    source_object_type = ""
    source_object_id = None
    if source_object is not None:
        source_object_type = source_object.__class__.__name__
        source_object_id = source_object.pk

    return AssetHistoryEntry.objects.create(
        asset=asset,
        occurred_at=occurred_at or timezone.now(),
        operator=operator,
        event_type=event_type,
        description=description,
        old_value="" if old_value is None else str(old_value),
        new_value="" if new_value is None else str(new_value),
        field_name=field_name,
        source_object_type=source_object_type,
        source_object_id=source_object_id,
    )


def capture_asset_history_values(asset):
    return {
        field_name: spec["value"](asset)
        for field_name, spec in ASSET_HISTORY_FIELD_SPECS.items()
    }


def record_asset_field_changes(*, asset, operator, before_values, source_object=None):
    from .models import AssetHistoryEntry

    entries = []
    for field_name, spec in ASSET_HISTORY_FIELD_SPECS.items():
        old_value = before_values.get(field_name, "")
        new_value = spec["value"](asset)
        if old_value == new_value:
            continue
        entries.append(
            record_asset_history(
                asset=asset,
                operator=operator,
                event_type=AssetHistoryEntry.EventType.UPDATED,
                description=spec["description"],
                old_value=old_value,
                new_value=new_value,
                field_name=field_name,
                source_object=source_object,
            )
        )
    return entries


def user_requires_asset_change_approval(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return True

    if getattr(user, "is_superuser", False):
        return False

    try:
        profile = user.profile
    except ObjectDoesNotExist:
        return True

    if profile.pk is None:
        return True

    if profile.can_approve_asset_changes:
        return False

    return profile.asset_changes_require_approval


def serialize_asset_form_payload(cleaned_data, *, exclude_system_managed=False):
    if exclude_system_managed:
        cleaned_data = _without_system_managed_asset_fields(cleaned_data)
    return {key: _serialize_payload_value(value) for key, value in cleaned_data.items()}


def deserialize_asset_payload_for_form(payload):
    from .forms import AssetForm, SYSTEM_MANAGED_ASSET_FIELDS

    allowed_fields = set(AssetForm.Meta.fields) - SYSTEM_MANAGED_ASSET_FIELDS
    return {
        key: value
        for key, value in payload.items()
        if key in allowed_fields
    }


def approve_asset_change_request(change_request, reviewer):
    if change_request.pk is None:
        raise ValidationError("Change request must be saved before approval.")

    from .forms import AssetForm
    from .models import AssetChangeRequest, AssetHistoryEntry

    with transaction.atomic():
        locked_request = AssetChangeRequest.objects.select_for_update().get(pk=change_request.pk)

        if locked_request.status != AssetChangeRequest.Status.PENDING:
            raise ValidationError("Request is not pending.")

        reviewer_is_global = _reviewer_has_global_asset_approval_access(reviewer)
        if not reviewer_is_global and not _reviewer_can_approve_asset_changes(reviewer):
            raise PermissionDenied("You do not have permission to approve asset changes.")

        if locked_request.operation == AssetChangeRequest.Operation.CREATE:
            if not reviewer_is_global:
                raise PermissionDenied("You do not have permission to approve asset creation without a location.")

            form_data = deserialize_asset_payload_for_form(locked_request.payload)
            form = AssetForm(data=form_data)
            if not form.is_valid():
                raise ValidationError(form.errors)

            asset = form.save()
            locked_request.asset = asset
            locked_request.status = AssetChangeRequest.Status.APPROVED
            locked_request.reviewed_by = reviewer
            locked_request.reviewed_at = timezone.now()
            locked_request.save(update_fields=["asset", "status", "reviewed_by", "reviewed_at", "updated_at"])
            record_asset_history(
                asset=asset,
                operator=reviewer,
                event_type=AssetHistoryEntry.EventType.CREATED,
                description="Utworzono środek po zatwierdzeniu zmiany",
                source_object=locked_request,
            )
            return asset

        if locked_request.operation == AssetChangeRequest.Operation.UPDATE:
            asset = _get_locked_asset_for_update_approval(locked_request)
            if not asset.is_active:
                raise ValidationError("Archived assets cannot be updated.")
            if not reviewer_is_global:
                _validate_reviewer_update_scope(reviewer, asset)

            payload = locked_request.payload
            if "current" not in payload:
                raise ValidationError("Update payload is missing current data.")
            if "proposed" not in payload:
                raise ValidationError("Update payload is missing proposed data.")

            actual_current = {
                field_name: getattr(asset, field_name)
                for field_name in AssetForm.Meta.fields
            }
            if serialize_asset_form_payload(
                actual_current,
                exclude_system_managed=True,
            ) != _without_system_managed_asset_fields(payload["current"]):
                raise ValidationError("Asset has changed since the request was created.")

            before_values = capture_asset_history_values(asset)
            form_data = deserialize_asset_payload_for_form(payload["proposed"])
            form = AssetForm(data=form_data, instance=asset)
            if not form.is_valid():
                raise ValidationError(form.errors)

            asset = form.save()
            locked_request.status = AssetChangeRequest.Status.APPROVED
            locked_request.reviewed_by = reviewer
            locked_request.reviewed_at = timezone.now()
            locked_request.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
            record_asset_field_changes(
                asset=asset,
                operator=reviewer,
                before_values=before_values,
                source_object=locked_request,
            )
            return asset

        raise ValidationError("Unsupported operation.")


def reject_asset_change_request(change_request, reviewer, comment=""):
    if change_request.pk is None:
        raise ValidationError("Change request must be saved before rejection.")

    from .models import AssetChangeRequest

    with transaction.atomic():
        locked_request = AssetChangeRequest.objects.select_for_update().get(pk=change_request.pk)

        if locked_request.status != AssetChangeRequest.Status.PENDING:
            raise ValidationError("Request is not pending.")

        if not (
            _reviewer_has_global_asset_approval_access(reviewer)
            or _reviewer_can_approve_asset_changes(reviewer)
        ):
            raise PermissionDenied("You do not have permission to reject asset changes.")

        locked_request.status = AssetChangeRequest.Status.REJECTED
        locked_request.reviewed_by = reviewer
        locked_request.reviewed_at = timezone.now()
        locked_request.review_comment = comment or ""
        locked_request.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_comment", "updated_at"])
        return locked_request


def _get_locked_asset_for_update_approval(change_request):
    if change_request.asset_id is None:
        raise ValidationError("Update request must reference an asset.")

    from .models import Asset

    try:
        return Asset.objects.select_for_update().get(pk=change_request.asset_id)
    except Asset.DoesNotExist:
        raise ValidationError("Update request must reference an existing asset.")


def _with_asset_payload_defaults(payload):
    if not isinstance(payload, dict):
        return payload

    payload = dict(payload)
    payload.setdefault("record_quantity", 1)
    return payload


def _without_system_managed_asset_fields(payload):
    if not isinstance(payload, dict):
        return payload

    from .forms import SYSTEM_MANAGED_ASSET_FIELDS

    return {
        key: value
        for key, value in payload.items()
        if key not in SYSTEM_MANAGED_ASSET_FIELDS
    }


def _validate_reviewer_update_scope(reviewer, asset):
    from accounts.utils import get_accessible_location_ids

    accessible_location_ids = get_accessible_location_ids(reviewer)
    if accessible_location_ids is None:
        return
    if asset.location_fk_id is None or asset.location_fk_id not in accessible_location_ids:
        raise PermissionDenied("You do not have permission to approve changes for this asset.")


def _reviewer_has_global_asset_approval_access(reviewer):
    if getattr(reviewer, "is_superuser", False):
        return True

    return False


def _reviewer_can_approve_asset_changes(reviewer):
    try:
        profile = reviewer.profile
    except ObjectDoesNotExist:
        return False

    return profile.pk is not None and profile.can_approve_asset_changes


def _serialize_payload_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, models.Model):
        return value.pk

    if isinstance(value, dict):
        return {key: _serialize_payload_value(item) for key, item in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_serialize_payload_value(item) for item in value]

    return str(value)

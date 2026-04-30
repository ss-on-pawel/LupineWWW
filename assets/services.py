from datetime import date, datetime
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import models, transaction
from django.utils import timezone


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


def serialize_asset_form_payload(cleaned_data):
    return {key: _serialize_payload_value(value) for key, value in cleaned_data.items()}


def deserialize_asset_payload_for_form(payload):
    from .forms import AssetForm

    allowed_fields = set(AssetForm.Meta.fields)
    return {
        key: value
        for key, value in payload.items()
        if key in allowed_fields
    }


def approve_asset_change_request(change_request, reviewer):
    if change_request.pk is None:
        raise ValidationError("Change request must be saved before approval.")

    from .forms import AssetForm
    from .models import AssetChangeRequest

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
            return asset

        if locked_request.operation == AssetChangeRequest.Operation.UPDATE:
            asset = _get_locked_asset_for_update_approval(locked_request)
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
            if serialize_asset_form_payload(actual_current) != payload["current"]:
                raise ValidationError("Asset has changed since the request was created.")

            form_data = deserialize_asset_payload_for_form(payload["proposed"])
            form = AssetForm(data=form_data, instance=asset)
            if not form.is_valid():
                raise ValidationError(form.errors)

            asset = form.save()
            locked_request.status = AssetChangeRequest.Status.APPROVED
            locked_request.reviewed_by = reviewer
            locked_request.reviewed_at = timezone.now()
            locked_request.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
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

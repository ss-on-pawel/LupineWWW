from django.core.exceptions import ValidationError
from django.db import transaction
from .models import Asset, AssetWithdrawal, AssetHistoryEntry
from .services import get_asset_withdraw_capabilities, record_asset_history


@transaction.atomic
def withdraw_asset(*, asset_id, operator, data, quantity=None):
    asset = Asset.objects.select_for_update(of=("self",)).select_related("asset_type_ref").get(pk=asset_id)
    previous = AssetWithdrawal.objects.filter(request_id=data["request_id"]).first()
    if previous:
        if (previous.asset_id != asset.pk or previous.operator_id != operator.pk
                or any(getattr(previous, key) != data[key] for key in ("method", "action_date", "notes", "document_number"))
                or (quantity is not None and previous.quantity != quantity)):
            raise ValidationError("Identyfikator operacji jest już używany.")
        return previous
    if not asset.is_active:
        raise ValidationError("Środek jest już wycofany z ewidencji.")
    before = asset.current_quantity
    if before != data["expected_quantity"]:
        raise ValidationError("Ilość zmieniła się. Otwórz formularz ponownie.")
    quantity = before if quantity is None else quantity
    if quantity < 0 or (quantity == 0 and before != 0) or quantity > before:
        raise ValidationError("Nieprawidłowa ilość do wycofania.")
    full = quantity == before
    if not full and not get_asset_withdraw_capabilities(asset)["can_partial_withdraw"]:
        raise ValidationError("Częściowe wycofanie jest dostępne tylko dla aktywnych środków ilościowych.")
    event = AssetWithdrawal.objects.create(asset=asset, operator=operator, method=data["method"],
        action_date=data["action_date"], notes=data["notes"], document_number=data["document_number"],
        quantity=quantity, quantity_before=before, quantity_after=before-quantity,
        is_full=full, request_id=data["request_id"])
    if full:
        asset.status = Asset.Status.LIQUIDATED
        asset.is_active = False
        asset.save(update_fields=["status", "is_active", "updated_at"])
    else:
        asset.current_quantity = before-quantity
        asset.save(update_fields=["current_quantity", "updated_at"])
    record_asset_history(asset=asset, operator=operator,
        event_type=AssetHistoryEntry.EventType.WITHDRAWN if full else AssetHistoryEntry.EventType.UPDATED,
        description=f"Wycofanie: {event.get_method_display()}, {event.action_date}, wycofano {quantity}",
        old_value="W ewidencji" if full else str(before),
        new_value="Wycofany" if full else str(before-quantity),
        field_name="is_active" if full else "current_quantity",
        source_object=event)
    return event

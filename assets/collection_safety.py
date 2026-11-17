"""Shared checks for ordinary asset operations touching a strict set.

QuerySet.update/bulk_update callers must invoke checks explicitly: Django does
not call model.save for these operations. Groups never inherit these limits.
"""
from django.core.exceptions import ValidationError
from contextlib import contextmanager
from contextvars import ContextVar

_authorized_set_operation = ContextVar('authorized_set_operation', default=frozenset())


@contextmanager
def authorized_set_state_change(asset_ids):
    """Internal capability used only after locking and scoping the complete set."""
    token = _authorized_set_operation.set(frozenset(asset_ids))
    try:
        yield
    finally:
        _authorized_set_operation.reset(token)


def strict_memberships(asset_ids):
    from .models import AssetCollectionMembership
    return AssetCollectionMembership.objects.filter(
        asset_id__in=asset_ids, left_at__isnull=True, collection__kind="set",
    )


def validate_asset_set_state(asset, *, update_fields=None):
    if not asset.pk:
        return
    if asset.pk in _authorized_set_operation.get():
        return
    member = strict_memberships([asset.pk]).first()
    if member is None:
        return
    fields = None if update_fields is None else set(update_fields)
    def changes(name):
        return fields is None or name in fields
    if changes("status") and asset.status == asset.Status.LIQUIDATED:
        raise ValidationError("Środek należy do zestawu. Najpierw rozstrzygnij wycofanie całego zestawu lub odłączenie elementu.")
    from .models import Asset
    previous_quantity = Asset.objects.filter(pk=asset.pk).values_list('current_quantity', flat=True).get()
    if changes("current_quantity") and asset.current_quantity != 1 and asset.current_quantity != previous_quantity:
        raise ValidationError("Kartoteka w zestawie musi zachować ilość 1. Wynik spisu zapisuje się oddzielnie.")
    if changes("record_quantity") and asset.record_quantity != 1:
        raise ValidationError("Ilość ewidencyjna kartoteki zestawu musi wynosić 1.")
    if changes("asset_type") or changes("asset_type_ref"):
        from .models import AssetTypeDictionary
        if asset.asset_type == asset.AssetType.QUANTITY or AssetTypeDictionary.objects.filter(
            code=asset.asset_type, is_quantity_based=True,
        ).exists():
            raise ValidationError("Elementów zestawu nie można zmienić na środki ilościowe.")
    if member.role == "member":
        if changes("purchase_value") and asset.purchase_value not in (None, 0):
            raise ValidationError("Wartość ewidencyjną zestawu prowadzi środek główny. Nie można nadać elementowi osobnej wartości.")
        if changes("inventory_number"):
            from .models import Asset
            original = Asset.objects.filter(pk=asset.pk).values_list("inventory_number", flat=True).get()
            if asset.inventory_number != original:
                raise ValidationError("Element dziedziczy numer zestawu. Własny numer można zmienić po odłączeniu.")


def validate_individual_withdrawal(asset_ids):
    if set(asset_ids).issubset(_authorized_set_operation.get()):
        return
    if strict_memberships(asset_ids).exists():
        raise ValidationError("Wybrane kartoteki należą do zestawu. Najpierw rozstrzygnij wycofanie całego zestawu lub odłączenie elementów.")


def validate_individual_move(asset_ids):
    # Moving physical components is legitimate, and records their real location.
    # Moving only the accounting parent would leave a misleading set location.
    if strict_memberships(asset_ids).filter(role="parent").exists():
        raise ValidationError("Przeniesienie środka nadrzędnego wymaga jawnego przeniesienia zestawu. Przenieś wybrane elementy fizyczne osobno.")


def validate_component_depreciation(asset_id):
    if strict_memberships([asset_id]).filter(role="member").exists():
        raise ValidationError("Element zestawu nie ma osobnego planu amortyzacji. Plan jest prowadzony na środku nadrzędnym.")


def accounting_document_assets(assets):
    """Collapse explicitly selected set members to their selected parent.

    Never silently add a parent outside the caller's selection/permission scope.
    Ordinary assets and members of organizational groups remain independent.
    """
    assets = list(assets)
    ids = {asset.pk for asset in assets}
    links = list(strict_memberships(ids).values('asset_id', 'collection_id', 'role'))
    if not links:
        return assets
    from .models import AssetCollectionMembership
    parents = dict(AssetCollectionMembership.objects.filter(
        collection_id__in={link['collection_id'] for link in links}, role='parent', left_at__isnull=True,
    ).values_list('collection_id', 'asset_id'))
    components = set()
    for link in links:
        if link['role'] == 'member':
            if parents.get(link['collection_id']) not in ids:
                raise ValidationError('Dla elementu zestawu zaznacz środek nadrzędny. Dokument LT jest wystawiany na zestaw, a nie osobno na element.')
            components.add(link['asset_id'])
    return [asset for asset in assets if asset.pk not in components]

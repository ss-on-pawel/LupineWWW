"""Transactional set/group operations preserving source identifiers and values.

Conversions of independently valued/amortized components require a separate
financial workflow and are rejected rather than modifying historical plans.
"""
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.utils import get_accessible_location_ids
from .models import (
    Asset, AssetChangeRequest, AssetCollection, AssetCollectionMembership,
    AssetCollectionEvent, AssetDepreciationPlan,
)
from .services import record_asset_history


def _actor(operator):
    if not getattr(operator, "is_authenticated", False) or not operator.is_active:
        raise PermissionDenied


def _ids(values):
    values = list(values)
    if not values or len(values) > 200:
        raise ValidationError("Wybierz od 1 do 200 środków.")
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValidationError("Nieprawidłowe identyfikatory środków.")
    return sorted(set(values))


def _key(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Nieprawidłowy identyfikator operacji.") from exc


def _assets(operator, ids, *, allow_archived=False):
    assets = list(Asset.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    allowed = get_accessible_location_ids(operator)
    if len(assets) != len(ids) or any(allowed is not None and a.location_fk_id not in allowed for a in assets):
        raise PermissionDenied("Brak dostępu do wszystkich środków operacji.")
    if not allow_archived and any(not asset.is_active for asset in assets):
        raise ValidationError("Archiwalne środki nie mogą zmieniać powiązań.")
    if AssetChangeRequest.objects.filter(asset_id__in=ids, status=AssetChangeRequest.Status.PENDING).exists():
        raise ValidationError("Najpierw rozstrzygnij oczekujące zmiany kartotek.")
    return assets


def _replay(key, operator, payload):
    event = AssetCollectionEvent.objects.filter(request_id=key).select_related("collection").first()
    if event is None:
        return None
    if event.operator_id != operator.pk or event.payload != payload:
        raise ValidationError("Identyfikator operacji został już użyty dla innych danych.")
    # Replays cannot bypass a user's current location scope.
    allowed = get_accessible_location_ids(operator)
    ids = {row["asset_id"] for row in event.before + event.after}
    if allowed is not None and Asset.objects.filter(pk__in=ids).exclude(location_fk_id__in=allowed).exists():
        raise PermissionDenied
    return event.collection


def _rows(collection):
    return list(collection.memberships.filter(left_at__isnull=True).order_by("asset_id").values("asset_id", "role"))


def _set_assets(assets, parent_id, *, existing_ids=()):
    if any(a.record_quantity != 1 or (a.current_quantity != 1 and not (
        a.pk in existing_ids and a.pk != parent_id and a.current_quantity == 0)) for a in assets):
        raise ValidationError("Zestaw wymaga pojedynczych środków o ilości 1.")
    from .models import AssetTypeDictionary
    quantity_codes = set(AssetTypeDictionary.objects.filter(is_quantity_based=True).values_list("code", flat=True))
    if any(a.asset_type == Asset.AssetType.QUANTITY or a.asset_type in quantity_codes for a in assets):
        raise ValidationError("Środki ilościowe mogą należeć do grupy, a nie do zestawu.")
    children = [a for a in assets if a.pk != parent_id]
    if any(a.purchase_value is not None and a.purchase_value != 0 for a in children):
        raise ValidationError("Dołączenie elementu z własną wartością wymaga uzgodnienia finansowego; użyj grupy.")
    if AssetDepreciationPlan.objects.filter(asset_id__in=[a.pk for a in children]).exists():
        raise ValidationError("Element ma własny plan amortyzacji; zachowaj go w grupie lub uzgodnij konwersję.")
    from inventory.models import InventorySnapshotItem, InventorySession
    if InventorySnapshotItem.objects.filter(asset_id_snapshot__in=[a.pk for a in assets], session__status=InventorySession.Status.ACTIVE).exists():
        raise ValidationError("Nie zmieniaj składu zestawu podczas otwartej inwentaryzacji.")


def _record(collection, operator, key, payload, before):
    after = _rows(collection)
    event = AssetCollectionEvent.objects.create(
        request_id=key, collection=collection, operator=operator,
        action=payload["action"], payload=payload, before=before, after=after,
    )
    before_by_id = {row['asset_id']: row for row in before}
    after_by_id = {row['asset_id']: row for row in after}
    if payload['action'].endswith('_set'):
        affected = set(before_by_id) | set(after_by_id)
    else:
        affected = {pk for pk in set(before_by_id) | set(after_by_id) if before_by_id.get(pk) != after_by_id.get(pk)}
        if collection.kind == 'set':
            affected.update(row['asset_id'] for row in before + after if row['role'] == 'parent')
    action_label = {
        'create': 'Utworzono powiązanie', 'attach': 'Dołączono elementy', 'detach': 'Odłączono elementy',
        'move_set': 'Przeniesiono cały zestaw', 'withdraw_set': 'Wycofano cały zestaw',
        'restore_set': 'Przywrócono cały zestaw', 'dissolve_set': 'Rozwiązano zestaw',
    }.get(payload['action'], 'Zmieniono powiązanie')
    def membership_label(row, rows):
        if row is None:
            return 'Środek samodzielny'
        role = 'środek nadrzędny' if row['role'] == 'parent' else ('element' if collection.kind == 'set' else 'członek')
        label = f'{collection.get_kind_display()} #{collection.pk} · {role}'
        if row['role'] == 'parent':
            label += f" · {sum(item['role'] == 'member' for item in rows)} elementów"
        return label
    for asset in Asset.objects.filter(pk__in=affected).order_by("pk"):
        record_asset_history(
            asset=asset, operator=operator, event_type="updated",
            description=f"{collection.get_kind_display()}: {collection.name} — {action_label}"[:255],
            field_name="collection", old_value=membership_label(before_by_id.get(asset.pk), before),
            new_value=membership_label(after_by_id.get(asset.pk), after), source_object=event,
        )
    return collection


@transaction.atomic
def create_collection(*, operator, kind, name, asset_ids, request_id, parent_id=None):
    _actor(operator)
    ids = _ids(asset_ids)
    key = _key(request_id)
    name = str(name or "").strip()
    if kind not in AssetCollection.Kind.values or not name or len(name) > 255:
        raise ValidationError("Wybierz rodzaj powiązania i podaj nazwę do 255 znaków.")
    if kind == AssetCollection.Kind.SET:
        if type(parent_id) is not int or parent_id not in ids or len(ids) < 2:
            raise ValidationError("Zestaw wymaga środka głównego i co najmniej jednego elementu.")
    elif parent_id is not None:
        raise ValidationError("Grupa nie ma środka nadrzędnego.")
    payload = dict(action="create", kind=kind, name=name, asset_ids=ids, parent_id=parent_id)
    assets = _assets(operator, ids)
    replay = _replay(key, operator, payload)
    if replay:
        return replay
    if AssetCollectionMembership.objects.filter(asset_id__in=ids, left_at__isnull=True).exists():
        raise ValidationError("Co najmniej jeden środek już należy do zestawu lub grupy.")
    if kind == AssetCollection.Kind.SET:
        _set_assets(assets, parent_id)
        name = next(a.name for a in assets if a.pk == parent_id)
    collection = AssetCollection.objects.create(kind=kind, name=name, created_by=operator)
    AssetCollectionMembership.objects.bulk_create([
        AssetCollectionMembership(collection=collection, asset=a, joined_by=operator,
            role=AssetCollectionMembership.Role.PARENT if a.pk == parent_id else AssetCollectionMembership.Role.MEMBER)
        for a in assets
    ])
    return _record(collection, operator, key, payload, [])


@transaction.atomic
def change_collection_members(*, operator, collection_id, asset_ids, action, expected_version, request_id):
    _actor(operator)
    ids = _ids(asset_ids)
    key = _key(request_id)
    if action not in {"attach", "detach"}:
        raise ValidationError("Nieprawidłowa operacja.")
    if type(expected_version) is not int or expected_version < 1:
        raise ValidationError("Nieprawidłowa wersja składu.")
    # Lock collection first; all mutations use the same order, then asset PK order.
    collection = AssetCollection.objects.select_for_update().get(pk=collection_id)
    before = _rows(collection)
    participant_ids = sorted(set(ids) | {r["asset_id"] for r in before})
    assets = _assets(operator, participant_ids)
    payload = dict(action=action, collection_id=collection.pk, asset_ids=ids, expected_version=expected_version)
    replay = _replay(key, operator, payload)
    if replay:
        return replay
    if collection.version != expected_version:
        raise ValidationError("Skład zmienił się. Otwórz formularz ponownie.")
    current_ids = {r["asset_id"] for r in before}
    parent_id = next((r["asset_id"] for r in before if r["role"] == "parent"), None)
    if action == "attach":
        if collection.kind == 'set' and len(current_ids | set(ids)) > 200:
            raise ValidationError('Zestaw może obejmować maksymalnie 200 kartotek, w tym środek nadrzędny.')
        if AssetCollectionMembership.objects.filter(asset_id__in=ids, left_at__isnull=True).exists():
            raise ValidationError("Środek już należy do zestawu lub grupy.")
    elif not set(ids) <= current_ids or parent_id in ids:
        raise ValidationError("Można odłączyć wyłącznie istniejące elementy; nie środek główny.")
    if collection.kind == AssetCollection.Kind.SET:
        if parent_id is None:
            raise ValidationError("Zestaw nie ma środka nadrzędnego.")
        _set_assets(assets, parent_id, existing_ids=current_ids)
        if action == "detach":
            parent = next(a for a in assets if a.pk == parent_id)
            if parent.purchase_value not in (None, 0) or AssetDepreciationPlan.objects.filter(asset=parent).exists():
                raise ValidationError("Odłączenie wymaga uzgodnienia wartości i amortyzacji zestawu.")
            if len(current_ids - set(ids)) < 2:
                raise ValidationError("Zestaw musi zachować przynajmniej jeden element. Aby odłączyć wszystkie kartoteki, użyj Rozwiąż zestaw.")
    if action == "attach":
        AssetCollectionMembership.objects.bulk_create([
            AssetCollectionMembership(collection=collection, asset_id=pk, joined_by=operator) for pk in ids
        ])
    else:
        collection.memberships.filter(asset_id__in=ids, left_at__isnull=True).update(left_at=timezone.now(), left_by=operator)
    collection.version += 1
    collection.save(update_fields=["version", "updated_at"])
    return _record(collection, operator, key, payload, before)


def effective_inventory_numbers(assets):
    """Return effective numbers in a bounded number of queries; never save Asset."""
    assets = list(assets)
    numbers = {a.pk: a.inventory_number for a in assets}
    memberships = list(AssetCollectionMembership.objects.filter(
        asset_id__in=numbers, left_at__isnull=True, role="member", collection__kind="set",
    ).values_list("asset_id", "collection_id"))
    parents = dict(AssetCollectionMembership.objects.filter(
        collection_id__in={collection_id for _, collection_id in memberships},
        role="parent", left_at__isnull=True,
    ).values_list("collection_id", "asset__inventory_number"))
    for asset_id, collection_id in memberships:
        if collection_id not in parents:
            raise ValidationError("Niekompletny zestaw: brak środka nadrzędnego.")
        numbers[asset_id] = parents[collection_id]
    return numbers


@transaction.atomic
def operate_set(*, operator, collection_id, action, expected_version, request_id, data):
    """Explicit whole-set operation: never inferred from selecting its parent."""
    _actor(operator)
    key = _key(request_id)
    if action not in {'move', 'withdraw', 'restore', 'dissolve'}:
        raise ValidationError('Nieprawidłowa operacja zestawu.')
    collection = AssetCollection.objects.select_for_update().get(pk=collection_id, kind='set')
    before = _rows(collection)
    if not before or sum(row['role'] == 'parent' for row in before) != 1:
        raise ValidationError('Niekompletny zestaw.')
    ids = sorted(row['asset_id'] for row in before)
    assets = _assets(operator, ids, allow_archived=True)
    payload = dict(action=action + '_set', collection_id=collection.pk,
        expected_version=expected_version, data=data)
    replay = _replay(key, operator, payload)
    if replay:
        return replay
    if type(expected_version) is not int or collection.version != expected_version:
        raise ValidationError('Stan zestawu zmienił się. Otwórz formularz ponownie.')
    if action == 'restore':
        if any(asset.is_active for asset in assets):
            raise ValidationError('Przywrócenie wymaga całego zestawu w archiwum.')
    elif any(not asset.is_active for asset in assets):
        raise ValidationError('Operacja wymaga całego zestawu w aktywnej ewidencji.')
    from .collection_safety import authorized_set_state_change
    from .models import AssetHistoryEntry
    with authorized_set_state_change(ids):
        if action == 'dissolve':
            parent_id = next(row['asset_id'] for row in before if row['role'] == 'parent')
            parent = next(asset for asset in assets if asset.pk == parent_id)
            _set_assets(assets, parent_id)
            if parent.purchase_value not in (None, 0) or AssetDepreciationPlan.objects.filter(asset=parent).exists():
                raise ValidationError('Rozwiązanie zestawu wymaga najpierw uzgodnienia wartości i planu amortyzacji.')
            collection.memberships.filter(left_at__isnull=True).update(left_at=timezone.now(), left_by=operator)
        elif action == 'move':
            from locations.models import Location
            target = Location.objects.filter(pk=data.get('target_location_id'), is_active=True).first()
            if target is None:
                raise ValidationError('Lokalizacja docelowa zmieniła się lub jest nieaktywna.')
            allowed = get_accessible_location_ids(operator)
            if allowed is not None and target.pk not in allowed:
                raise PermissionDenied
            path = target.path
            for asset in assets:
                old = asset.location
                asset.location_fk = target
                asset.location = path
                asset.save(update_fields=['location_fk', 'location', 'updated_at'])
                record_asset_history(asset=asset, operator=operator, event_type='moved',
                    description='Przeniesiono cały zestaw', field_name='location_fk', old_value=old, new_value=path)
        elif action == 'withdraw':
            from datetime import date
            from .withdrawals import withdraw_asset
            for asset in assets:
                item_data = dict(data, action_date=date.fromisoformat(data['action_date']),
                    request_id=uuid.uuid5(key, str(asset.pk)), expected_quantity=asset.current_quantity)
                withdraw_asset(asset_id=asset.pk, operator=operator, data=item_data)
        else:
            for asset in assets:
                asset.status = Asset.Status.ACTIVE
                asset.is_active = True
                asset.save(update_fields=['status', 'is_active', 'updated_at'])
                record_asset_history(asset=asset, operator=operator, event_type=AssetHistoryEntry.EventType.RESTORED,
                    description='Przywrócono cały zestaw', field_name='is_active', old_value='Archiwum', new_value='Aktywna ewidencja')
    collection.version += 1
    collection.save(update_fields=['version', 'updated_at'])
    return _record(collection, operator, key, payload, before)

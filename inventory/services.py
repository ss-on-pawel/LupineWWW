from __future__ import annotations

from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from assets.models import Asset
from locations.models import Location

from .models import (
    InventoryObservedItem,
    InventoryScanBatch,
    InventorySession,
    InventorySnapshotItem,
)


SESSION_NUMBER_PREFIX = "INV-"
SESSION_NUMBER_WIDTH = 6


@transaction.atomic
def start_inventory_session(*, created_by, root_locations, asset_types):
    roots = list(root_locations)
    asset_type_values = list(dict.fromkeys(asset_types))

    session = InventorySession.objects.create(
        number=_get_next_session_number(),
        created_by=created_by,
        asset_type_scope=asset_type_values,
    )
    session.scope_root_locations.set(roots)

    location_ids = _get_location_subtree_ids(roots)
    assets = (
        Asset.objects
        .select_related("location_fk")
        .filter(location_fk_id__in=location_ids, asset_type__in=asset_type_values)
        .order_by("id")
    )

    InventorySnapshotItem.objects.bulk_create(
        [_build_snapshot_item(session, asset) for asset in assets],
        batch_size=500,
    )
    return session


@transaction.atomic
def import_inventory_scan_text(raw_text: str, uploaded_by=None) -> InventoryScanBatch:
    lines = [line.strip() for line in str(raw_text).splitlines() if line.strip()]
    if not lines:
        raise ValueError("Import text is empty.")

    session_number = lines[0]
    try:
        session = InventorySession.objects.select_for_update().get(number=session_number)
    except InventorySession.DoesNotExist as exc:
        raise ValueError("Inventory session does not exist.") from exc

    if session.status != InventorySession.Status.ACTIVE:
        raise ValueError("Inventory session is not active.")

    batch = InventoryScanBatch.objects.create(
        session=session,
        uploaded_by=uploaded_by,
        raw_text=raw_text,
        total_lines=len(lines),
    )

    now = timezone.now()
    current_location = None
    processed_lines = 0
    recognized_assets_count = 0
    unknown_codes_count = 0

    for code in lines[1:]:
        location = _get_location_by_code(code)
        if location is not None:
            current_location = location
            processed_lines += 1
            continue

        asset = _get_asset_by_scan_code(code)
        if asset is None:
            InventoryObservedItem.objects.create(
                session=session,
                asset=None,
                code=code,
                scanned_location=current_location,
                status=InventoryObservedItem.Status.UNKNOWN_CODE,
                first_seen_at=now,
                last_seen_at=now,
            )
            unknown_codes_count += 1
            processed_lines += 1
            continue

        status = _resolve_observed_status(session, asset, current_location)
        observed_item, created = InventoryObservedItem.objects.get_or_create(
            session=session,
            asset=asset,
            defaults={
                "code": code,
                "scanned_location": current_location,
                "status": status,
                "first_seen_at": now,
                "last_seen_at": now,
            },
        )
        if not created:
            observed_item.code = code
            observed_item.scanned_location = current_location
            observed_item.status = status
            observed_item.last_seen_at = now
            observed_item.save(update_fields=["code", "scanned_location", "status", "last_seen_at"])

        recognized_assets_count += 1
        processed_lines += 1

    batch.processed_lines = processed_lines
    batch.recognized_assets_count = recognized_assets_count
    batch.unknown_codes_count = unknown_codes_count
    batch.processed_at = timezone.now()
    batch.save(
        update_fields=[
            "processed_lines",
            "recognized_assets_count",
            "unknown_codes_count",
            "processed_at",
        ]
    )
    return batch


def _get_location_by_code(code: str):
    if not code.startswith("LOC-"):
        return None
    return Location.objects.filter(code=code).first()


def _get_asset_by_scan_code(code: str):
    asset = Asset.objects.filter(barcode=code).order_by("id").first()
    if asset is not None:
        return asset
    return Asset.objects.filter(inventory_number=code).order_by("id").first()


def _resolve_observed_status(session: InventorySession, asset: Asset, current_location: Location | None) -> str:
    snapshot = InventorySnapshotItem.objects.filter(
        session=session,
        asset_id_snapshot=asset.id,
    ).first()
    if snapshot is None:
        return InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE
    if current_location is not None and snapshot.location_fk_id_snapshot == current_location.id:
        return InventoryObservedItem.Status.FOUND_OK
    return InventoryObservedItem.Status.FOUND_OTHER_LOCATION


def _get_next_session_number() -> str:
    latest_session = InventorySession.objects.select_for_update().order_by("-id").first()
    next_sequence = _parse_session_sequence(latest_session.number) + 1 if latest_session else 1

    while True:
        number = f"{SESSION_NUMBER_PREFIX}{next_sequence:0{SESSION_NUMBER_WIDTH}d}"
        if not InventorySession.objects.filter(number=number).exists():
            return number
        next_sequence += 1


def _parse_session_sequence(number: str) -> int:
    if not number.startswith(SESSION_NUMBER_PREFIX):
        return 0
    try:
        return int(number.removeprefix(SESSION_NUMBER_PREFIX))
    except ValueError:
        return 0


def _get_location_subtree_ids(root_locations) -> set[int]:
    root_ids = {location.id for location in root_locations if location.id is not None}
    if not root_ids:
        return set()

    children_by_parent_id = defaultdict(list)
    for location_id, parent_id in Location.objects.values_list("id", "parent_id"):
        children_by_parent_id[parent_id].append(location_id)

    location_ids = set()
    pending_ids = list(root_ids)
    while pending_ids:
        location_id = pending_ids.pop()
        if location_id in location_ids:
            continue
        location_ids.add(location_id)
        pending_ids.extend(children_by_parent_id.get(location_id, ()))
    return location_ids


def _build_snapshot_item(session: InventorySession, asset: Asset) -> InventorySnapshotItem:
    location = asset.location_fk
    return InventorySnapshotItem(
        session=session,
        asset=asset,
        asset_id_snapshot=asset.id,
        inventory_number=asset.inventory_number,
        name=asset.name,
        barcode=asset.barcode,
        asset_type=asset.asset_type,
        asset_type_display=asset.get_asset_type_display(),
        location_fk_id_snapshot=location.id,
        location_code=location.code,
        location_name=location.name,
        location_path=location.path,
        status_snapshot=asset.status,
    )

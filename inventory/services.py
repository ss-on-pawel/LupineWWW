from __future__ import annotations

from collections import defaultdict
from itertools import islice

from django.core.exceptions import ValidationError

from django.db import connection, transaction
from django.utils import timezone

from assets.models import Asset, ScanCode
from locations.models import Location

from .models import (
    InventoryObservedItem,
    InventoryRawScan,
    InventoryScanBatch,
    InventoryScanTerminal,
    InventorySession,
    InventorySnapshotItem,
)


from .scope import UNASSIGNED_ASSET_TYPE_LABEL


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

    # Read the tree once; reuse it for scope and all snapshot paths.
    nodes = {pk: (name, parent) for pk, name, parent in
             Location.objects.values_list("id", "name", "parent_id")}
    location_ids = _get_location_subtree_ids(roots, nodes=nodes)
    paths = _build_location_paths(nodes, location_ids)
    assets = (
        Asset.objects
        .select_related("location_fk", "asset_type_ref")
        .filter(location_fk_id__in=location_ids, asset_type__in=asset_type_values, is_active=True)
        .only("id", "inventory_number", "name", "barcode", "asset_type",
              "current_quantity", "purchase_value", "status", "asset_type_ref__name",
              "location_fk__id", "location_fk__code", "location_fk__name")
        .order_by("id")
    )

    rows = (_build_snapshot_item(session, asset, location_paths=paths)
            for asset in assets.iterator(chunk_size=500))
    while batch := list(islice(rows, 500)):
        InventorySnapshotItem.objects.bulk_create(batch, batch_size=500)
    return session


@transaction.atomic
def import_inventory_scan_text(raw_text: str, uploaded_by=None, allowed_location_ids=None) -> InventoryScanBatch:
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

    allowed = allowed_location_ids
    for code in lines[1:]:
        error = _scan_location_error(code, allowed)
        if error:
            raise ValueError(error)
        location = _get_location_by_code(code)
        if location is not None:
            current_location = location
            processed_lines += 1
            continue

        asset = _get_asset_by_scan_code(code)
        if asset is None:
            _record_unknown_code(session=session, code=code, current_location=current_location, seen_at=now)
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


class ScanContext:
    """Request-local lookup data. Never reused across accounts or requests."""
    def __init__(self, session, codes):
        self.session_id = session.pk
        self.entries = {entry.code: entry for entry in ScanCode.objects.filter(code__in=codes)}
        self.locations = {location.code: location for location in Location.objects.filter(code__in=codes)}
        self.assets = {asset.barcode: asset for asset in Asset.objects.filter(barcode__in=codes, is_active=True).only('id', 'barcode')}
        self.snapshots = dict(InventorySnapshotItem.objects.filter(session=session,
            asset_id_snapshot__in=[asset.pk for asset in self.assets.values()]).values_list('asset_id_snapshot', 'location_fk_id_snapshot'))


@transaction.atomic
def record_mobile_scan(session, code, current_location_code, allowed_location_ids=None):
    locked = InventorySession.objects.select_for_update().get(pk=session.pk)
    if locked.status != InventorySession.Status.ACTIVE:
        return {'ok': False, 'type': 'scan_error', 'error': 'Sesja jest zamknięta.'}
    return _record_scan_observation(locked, code, current_location_code, allowed_location_ids)


def _record_scan_observation(session: "InventorySession", code: str, current_location_code: str | None, allowed_location_ids=None, scan_context=None) -> dict:
    """Record a single barcode scan from the mobile scanner endpoint."""
    now = timezone.now()
    error = _scan_location_error(code, allowed_location_ids, scan_context)
    if not error and current_location_code:
        error = _scan_location_error(current_location_code, allowed_location_ids, scan_context)
        if not error and _get_location_by_code(current_location_code, scan_context) is None:
            error = 'Nieznany kod bieżącej lokalizacji. Zeskanuj lokalizację ponownie.'
    if error:
        return {'ok': False, 'type': 'scan_error', 'error': error}
    location = _get_location_by_code(code, scan_context)
    if location is not None:
        return {'ok': True, 'type': 'location', 'location_code': location.code, 'location_name': location.name}
    current_location = None
    if current_location_code:
        current_location = _get_location_by_code(current_location_code, scan_context)

    asset = _get_asset_by_scan_code(code, scan_context)
    if asset is None:
        _record_unknown_code(session=session, code=code, current_location=current_location, seen_at=now)
        return {"ok": True, "type": "unknown", "status": InventoryObservedItem.Status.UNKNOWN_CODE}

    status = _resolve_observed_status(session, asset, current_location, scan_context)
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

    return {"ok": True, "type": "asset", "status": status}


@transaction.atomic
def record_terminal_raw_scan(
    *,
    session: InventorySession,
    terminal: InventoryScanTerminal,
    client_scan_id: str,
    code: str,
    scanned_at,
    current_location_code: str | None = None,
    allowed_location_ids=None,
    update_terminal=True,
    scan_context=None,
) -> tuple[InventoryRawScan, bool]:
    """Store one terminal scan idempotently and update the inventory result."""
    session = InventorySession.objects.select_for_update().get(pk=session.pk)
    if session.status != InventorySession.Status.ACTIVE:
        raise ValueError('Sesja jest zamknięta.')
    if terminal.session_id != session.pk or not terminal.is_active:
        raise ValueError('Terminal jest nieaktywny lub należy do innej sesji.')
    if scan_context is not None and scan_context.session_id != session.pk:
        raise ValueError('Kontekst pochodzi z innej sesji.')
    normalized_code = str(code or "").strip()
    normalized_location_code = str(current_location_code or "").strip()
    raw_scan, created = InventoryRawScan.objects.get_or_create(
        terminal=terminal,
        client_scan_id=str(client_scan_id or "").strip(),
        defaults={
            "session": session,
            "code": normalized_code,
            "current_location": _get_location_by_code(normalized_location_code, scan_context) if normalized_location_code else None,
            "current_location_code": normalized_location_code,
            "scanned_at": scanned_at,
        },
    )
    if not created and (raw_scan.code != normalized_code or raw_scan.current_location_code != normalized_location_code):
        raise ValueError('Identyfikator skanu został już użyty z innymi danymi.')
    if created:
        process_terminal_raw_scan(raw_scan, allowed_location_ids=allowed_location_ids,
                                  scan_context=scan_context, locked_session=session)
    if update_terminal:
        terminal.last_seen_at = timezone.now()
        terminal.save(update_fields=["last_seen_at", "updated_at"])
    return raw_scan, created


@transaction.atomic
def process_terminal_raw_scan(raw_scan: InventoryRawScan, allowed_location_ids=None, *, scan_context=None, locked_session=None) -> dict:
    code = (raw_scan.code or "").strip()
    now = timezone.now()
    session = locked_session or InventorySession.objects.select_for_update().get(pk=raw_scan.session_id)
    if session.status != InventorySession.Status.ACTIVE:
        raise ValueError('Sesja jest zamknięta.')
    result = _record_scan_observation(session, code, raw_scan.current_location_code or None, allowed_location_ids, scan_context)
    raw_scan.processing_status = (InventoryRawScan.ProcessingStatus.LOCATION if result['type'] == 'location' else
                                 InventoryRawScan.ProcessingStatus.PROCESSED if result['ok'] else InventoryRawScan.ProcessingStatus.ERROR)
    raw_scan.processing_result = result
    raw_scan.processed_at = now
    raw_scan.save(update_fields=["processing_status", "processing_result", "processed_at"])
    return result


def _record_unknown_code(
    *,
    session: InventorySession,
    code: str,
    current_location: Location | None,
    seen_at,
) -> InventoryObservedItem:
    observed_item = (
        InventoryObservedItem.objects
        .filter(
            session=session,
            asset__isnull=True,
            code=code,
            status=InventoryObservedItem.Status.UNKNOWN_CODE,
        )
        .order_by("id")
        .first()
    )
    if observed_item is None:
        return InventoryObservedItem.objects.create(
            session=session,
            asset=None,
            code=code,
            scanned_location=current_location,
            status=InventoryObservedItem.Status.UNKNOWN_CODE,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
        )

    observed_item.scanned_location = current_location
    observed_item.last_seen_at = seen_at
    observed_item.save(update_fields=["scanned_location", "last_seen_at"])
    return observed_item


def _get_location_by_code(code: str, scan_context=None):
    if scan_context is not None:
        location = scan_context.locations.get(code)
        return location if location is not None and location.is_active else None
    return Location.objects.filter(code=code, is_active=True).first()


def _scan_location_error(code, allowed_location_ids=None, scan_context=None):
    entry = scan_context.entries.get(code) if scan_context is not None else ScanCode.objects.filter(pk=code).first()
    if entry and entry.asset_id is not None and entry.location_id is not None:
        return 'Kod niejednoznaczny: środek i lokalizacja mają ten sam kod. Wyjaśnij kolizję w bazie.'
    if entry is None or entry.location_id is None:
        return None
    location = (scan_context.locations.get(code) if scan_context is not None else
                Location.objects.filter(pk=entry.location_id, code=code).only('id', 'is_active').first())
    if location and (not location.is_active or (allowed_location_ids is not None and location.pk not in allowed_location_ids)):
        return 'Lokalizacja jest nieaktywna lub poza zakresem dostępu.'
    return None


def _get_asset_by_scan_code(code: str, scan_context=None):
    if scan_context is not None:
        return scan_context.assets.get(code)
    return Asset.objects.filter(barcode=code, is_active=True).order_by("id").first()


def _resolve_observed_status(session: InventorySession, asset: Asset, current_location: Location | None, scan_context=None) -> str:
    if scan_context is not None:
        if asset.pk not in scan_context.snapshots:
            return InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE
        return (InventoryObservedItem.Status.FOUND_OK if current_location is not None and
                scan_context.snapshots[asset.pk] == current_location.pk else InventoryObservedItem.Status.FOUND_OTHER_LOCATION)
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
    # Lock a stable key even when no session exists. Locking the last row alone
    # can leave a waiter reading the previous last row after another start commits.
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(1280659017, 1)')
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


def _get_location_subtree_ids(root_locations, *, nodes=None) -> set[int]:
    root_ids = {location.id for location in root_locations if location.id is not None}
    if not root_ids:
        return set()

    children_by_parent_id = defaultdict(list)
    rows = (Location.objects.values_list("id", "parent_id") if nodes is None else
            ((pk, node[1]) for pk, node in nodes.items()))
    for location_id, parent_id in rows:
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


def _build_location_paths(nodes, location_ids):
    paths = {}
    for location_id in location_ids:
        trail = []
        visited = set()
        current = location_id
        while current is not None and current not in paths:
            if current in visited:
                raise ValidationError("Cykl w drzewie lokalizacji. Nie utworzono spisu.")
            if current not in nodes:
                raise ValidationError("Niepełne drzewo lokalizacji. Nie utworzono spisu.")
            visited.add(current)
            trail.append(current)
            current = nodes[current][1]
        prefix = paths.get(current, "")
        for pk in reversed(trail):
            prefix = f"{prefix} / {nodes[pk][0]}" if prefix else nodes[pk][0]
            paths[pk] = prefix
    return paths


def _build_snapshot_item(session: InventorySession, asset: Asset, *, location_paths=None) -> InventorySnapshotItem:
    location = asset.location_fk
    return InventorySnapshotItem(
        session=session,
        asset=asset,
        asset_id_snapshot=asset.id,
        inventory_number=asset.inventory_number,
        name=asset.name,
        barcode=asset.barcode,
        asset_type=asset.asset_type,
        asset_type_display=(asset.asset_type_ref.name if asset.asset_type_ref_id else asset.get_asset_type_display()) if asset.asset_type else UNASSIGNED_ASSET_TYPE_LABEL,
        # Legacy column name; stores the expected quantity copied from Asset.current_quantity.
        record_quantity_snapshot=asset.current_quantity,
        purchase_value_snapshot=asset.purchase_value,
        location_fk_id_snapshot=location.id,
        location_code=location.code,
        location_name=location.name,
        location_path=location.path if location_paths is None else location_paths[location.id],
        status_snapshot=asset.status,
    )

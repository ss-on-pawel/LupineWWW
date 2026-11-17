"""Strict-set metadata is frozen once, with the inventory session's assets."""
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction

from assets.models import AssetCollection, AssetCollectionMembership
from .models import InventorySnapshotItem


def lock_inventory_collections(assets):
    collection_ids = AssetCollectionMembership.objects.filter(
        asset_id__in=assets.values('pk'), left_at__isnull=True, collection__kind='set',
    ).values('collection_id')
    # Same collection->asset lock order used by membership changes.
    list(AssetCollection.objects.select_for_update().filter(pk__in=collection_ids).order_by('pk').values_list('pk', flat=True))


def freeze_inventory_collections(session):
    snapshot_ids = session.snapshot_items.values('asset_id_snapshot')
    collection_ids = AssetCollectionMembership.objects.filter(
        asset_id__in=snapshot_ids, left_at__isnull=True, collection__kind='set',
    ).values('collection_id')
    links = list(AssetCollectionMembership.objects.filter(collection_id__in=collection_ids,
        left_at__isnull=True).select_related('collection', 'asset').order_by('asset_id'))
    grouped = defaultdict(list)
    for link in links:
        grouped[link.collection_id].append(link)
    snapshots = {row.asset_id_snapshot: row for row in session.snapshot_items.filter(
        asset_id_snapshot__in=[link.asset_id for link in links])}
    changed = []
    for members in grouped.values():
        parents = [link for link in members if link.role == 'parent']
        children = [link for link in members if link.role == 'member']
        if len(parents) != 1 or not children:
            raise ValidationError('Niekompletny skład zestawu; nie rozpoczęto spisu.')
        parent = parents[0]
        common = dict(id=parent.collection_id, version=parent.collection.version,
            parent_id=parent.asset_id, name=parent.asset.name,
            expected_ids=sorted(link.asset_id for link in children))
        for link in members:
            row = snapshots.get(link.asset_id)
            if row is None:
                continue
            row.collection_snapshot = dict(common, role=link.role, current_quantity_at_start=row.record_quantity_snapshot)
            row.record_quantity_snapshot = 1
            row.inventory_number = parent.asset.inventory_number
            # Components do not carry independent accounting value.
            if link.role == 'member':
                row.purchase_value_snapshot = 0
            changed.append(row)
    if changed:
        InventorySnapshotItem.objects.bulk_update(changed,
            ['collection_snapshot', 'inventory_number', 'purchase_value_snapshot', 'record_quantity_snapshot'], batch_size=500)


def split_inventory_collections(work_items):
    """Physical items exclude accounting parents. Aggregates never use live links.

    Partial scope remains unresolved, even when all *visible* children match.
    """
    physical, parents = [], []
    children = defaultdict(dict)
    for item in work_items:
        meta = item['snapshot'].collection_snapshot
        if meta and meta.get('role') == 'parent':
            parents.append(item)
        else:
            physical.append(item)
            if meta:
                children[meta['id']][item['snapshot'].asset_id_snapshot] = item
    summaries = []
    meta_by_id = {}
    for item in work_items:
        meta = item['snapshot'].collection_snapshot
        if meta:
            meta_by_id[meta['id']] = meta
    parent_by_id = {item['snapshot'].collection_snapshot['id']: item for item in parents}
    for key, meta in meta_by_id.items():
        visible = children.get(key, {})
        expected = set(meta['expected_ids'])
        partial = not expected.issubset(visible)
        found = [item for item in visible.values() if item['actual_quantity'] > 0]
        missing = [item['snapshot'].name for item in visible.values() if item['actual_quantity'] == 0]
        wrong = any(item['row_status'] == 'wrong_location' for item in found)
        complete = not partial and not missing
        status = 'Niepełny zakres' if partial else ('Niekompletny' if missing else ('Kompletny — inna lokalizacja' if wrong else 'Kompletny'))
        summaries.append(dict(id=key, name=meta['name'], complete=complete, partial=partial,
            matching=complete and not wrong, found=len(found), expected=len(expected),
            missing=missing, status=status, parent_item=parent_by_id.get(key)))
    return physical, parents, summaries


def validate_inventory_collection_versions(session, *, lock=True):
    frozen_rows = dict(session.snapshot_items.exclude(collection_snapshot={}).values_list('asset_id_snapshot', 'collection_snapshot'))
    expected = {meta['id']: meta['version'] for meta in frozen_rows.values()}
    links = {asset_id: (collection_id, role) for asset_id, collection_id, role in
        AssetCollectionMembership.objects.filter(left_at__isnull=True, collection__kind='set',
            asset_id__in=session.snapshot_items.values('asset_id_snapshot')).values_list('asset_id', 'collection_id', 'role')}
    query = AssetCollection.objects.filter(pk__in=set(expected) | {collection_id for collection_id, _ in links.values()}).order_by('pk')
    if lock:
        query = query.select_for_update()
    current = dict(query.values_list('pk', 'version'))
    if any(current.get(key) != version for key, version in expected.items()):
        raise ValidationError('Po rozpoczęciu spisu zmienił się skład zestawu. Wynik wymaga uzgodnienia.')
    for asset_id in set(frozen_rows) | set(links):
        frozen = frozen_rows.get(asset_id)
        expected_link = (frozen['id'], frozen['role']) if frozen else None
        if links.get(asset_id) != expected_link:
            raise ValidationError('Po rozpoczęciu spisu zmieniło się powiązanie środka z zestawem. Wynik wymaga uzgodnienia.')

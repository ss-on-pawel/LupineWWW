"""PostgreSQL read-only aggregates. Work items and reports keep their existing semantics."""
from django.db import connection
from accounts.utils import get_accessible_location_ids


def inventory_summary(session, user):
    if connection.vendor != 'postgresql':
        return None
    allowed = get_accessible_location_ids(user)
    scope = '' if allowed is None else 'AND s.location_fk_id_snapshot = ANY(%(allowed)s)'
    observed_scope = '' if allowed is None else '''AND (
        o.asset_id IN (SELECT asset_id_snapshot FROM scoped)
        OR a.location_fk_id = ANY(%(allowed)s)
        OR (o.asset_id IS NULL AND o.scanned_location_id = ANY(%(allowed)s)))'''
    # Match Python str.splitlines()/str.strip(), including its Unicode whitespace.
    whitespace = ''.join(chr(c) for c in [9,10,11,12,13,28,29,30,31,32,133,160,5760,
                                         *range(8192,8203),8232,8233,8239,8287,12288])
    separators = '[' + ''.join(chr(c) for c in [10,11,12,13,28,29,30,133,8232,8233]) + ']'
    sql = f'''
    WITH scoped AS (
        SELECT s.* FROM inventory_inventorysnapshotitem s WHERE s.session_id=%(session)s {scope}
        AND COALESCE(s.collection_snapshot->>'role', '') <> 'parent'
    ), file_lines AS (
        SELECT btrim(line, %(whitespace)s) AS code,
               row_number() OVER (PARTITION BY b.id ORDER BY ordinal) AS line_number
        FROM inventory_inventoryscanbatch b
        CROSS JOIN LATERAL regexp_split_to_table(b.raw_text, %(separators)s) WITH ORDINALITY AS l(line, ordinal)
        WHERE b.session_id=%(session)s AND btrim(line, %(whitespace)s)<>''
    ), event_counts AS (
        SELECT code, count(*) AS n FROM file_lines WHERE line_number>1 GROUP BY code
        UNION ALL
        SELECT code, count(*) AS n FROM inventory_inventoryrawscan
        WHERE session_id=%(session)s AND processing_status NOT IN ('location','error') GROUP BY code
    ), counts AS (
        SELECT e.code, sum(e.n)::bigint AS n FROM event_counts e
        WHERE e.code<>'' AND NOT EXISTS (SELECT 1 FROM assets_scancode c WHERE c.code=e.code AND c.location_id IS NOT NULL)
        GROUP BY e.code
    ), basis AS (
        SELECT s.id, s.asset_id_snapshot, COALESCE(s.record_quantity_snapshot,a.current_quantity,1)::bigint AS expected,
            COALESCE(t.is_quantity_based,false) AS quantity_based,
            o.status AS observed_status, o.id IS NOT NULL AS observed,
            CASE WHEN COALESCE(t.is_quantity_based,false) THEN COALESCE(c.n,0)
                 WHEN o.id IS NOT NULL THEN 1 ELSE 0 END::bigint AS read_quantity,
            COALESCE(m.quantity,0)::bigint AS manual_quantity,
            f.id IS NOT NULL AS confirmed
        FROM scoped s
        LEFT JOIN assets_asset a ON a.id=s.asset_id
        LEFT JOIN assets_assettypedictionary t ON t.code=s.asset_type
        LEFT JOIN inventory_inventoryobserveditem o ON o.session_id=s.session_id AND o.asset_id=s.asset_id_snapshot
        LEFT JOIN counts c ON c.code=s.barcode
        LEFT JOIN inventory_inventorysessionmanualquantity m ON m.session_id=s.session_id AND m.asset_id=s.asset_id_snapshot
        LEFT JOIN inventory_inventorysessionmanualconfirmation f ON f.session_id=s.session_id AND f.asset_id=s.asset_id_snapshot
    ), computed AS (
        SELECT *, (quantity_based OR expected>1) AS uses_quantity,
            CASE WHEN quantity_based OR expected>1 THEN read_quantity+manual_quantity
                 WHEN observed OR confirmed THEN 1 ELSE 0 END AS actual
        FROM basis
    ), visible_observations AS (
        SELECT o.status FROM inventory_inventoryobserveditem o LEFT JOIN assets_asset a ON a.id=o.asset_id
        WHERE o.session_id=%(session)s {observed_scope}
    )
    SELECT count(*) AS snapshot_total,
        count(*) FILTER (WHERE actual>0) AS read_count,
        count(*) FILTER (WHERE actual=expected) AS matching_count,
        count(*) FILTER (WHERE actual<expected) AS shortage_count,
        count(*) FILTER (WHERE actual>expected) AS surplus_count,
        count(*) FILTER (WHERE actual=0) AS no_read_count,
        count(*) FILTER (WHERE observed_status='found_other_location') AS wrong_location_count,
        count(*) FILTER (WHERE NOT uses_quantity AND confirmed) AS manual_confirmation_count,
        count(*) FILTER (WHERE uses_quantity AND actual<>expected) AS quantity_difference_count,
        (SELECT count(*) FROM visible_observations WHERE status='unknown_code') AS unknown_code_count,
        (SELECT count(*) FROM visible_observations WHERE status='found_out_of_scope') AS found_out_of_scope_count
    FROM computed
    '''
    with connection.cursor() as cursor:
        cursor.execute(sql, {'session':session.pk, 'allowed':list(allowed or []),
                             'whitespace':whitespace, 'separators':separators})
        return dict(zip([column[0] for column in cursor.description],cursor.fetchone()))

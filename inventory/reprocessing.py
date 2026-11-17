"""Explicit recovery for a stored failed event; normal retry never reinterprets it."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from accounts.utils import get_accessible_location_ids
from .models import InventoryRawScan, InventorySession
from .services import process_terminal_raw_scan


@transaction.atomic
def reprocess_failed_scan(raw_scan_id, operator):
    from .views import _can_manage_inventory_session
    if not getattr(operator, 'is_authenticated', False):
        raise PermissionDenied
    original = InventoryRawScan.objects.get(pk=raw_scan_id)
    session = InventorySession.objects.select_for_update().get(pk=original.session_id)
    if not _can_manage_inventory_session(operator, session):
        raise PermissionDenied
    if session.status != InventorySession.Status.ACTIVE:
        raise ValidationError('Nie można przetwarzać skanów zamkniętej sesji.')
    raw = InventoryRawScan.objects.select_for_update().get(pk=raw_scan_id)
    if raw.processing_status != InventoryRawScan.ProcessingStatus.ERROR:
        raise ValidationError('Ponowne przetwarzanie dotyczy wyłącznie błędnych skanów.')
    previous = raw.processing_result
    allowed = get_accessible_location_ids(operator)
    if raw.terminal.source == 'phone':
        from .services import _get_location_subtree_ids
        allowed = _get_location_subtree_ids(session.scope_root_locations.all())
    result = process_terminal_raw_scan(raw, allowed, locked_session=session)
    history = list(previous.get('reprocessing_history', []))
    history.append({'operator_id': operator.pk, 'at': timezone.now().isoformat(),
                    'previous_error': previous.get('error', '')})
    raw.processing_result = dict(result, reprocessing_history=history)
    raw.save(update_fields=['processing_result'])
    return raw

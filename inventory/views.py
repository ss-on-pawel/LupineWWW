import json
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from django.utils.decorators import method_decorator
from django.views.generic import DetailView, ListView, TemplateView, View

from accounts.utils import get_accessible_location_ids
from assets.models import Asset, AssetHistoryEntry, AssetTypeDictionary, ScanCode
from config.features import FeatureRequiredMixin, require_feature
from locations.models import Location

from .forms import DEFAULT_ASSET_TYPES, InventorySessionStartForm, SimpleInventorySessionStartForm
from .scope import get_inventory_asset_type_choices
from .models import (
    InventoryObservedItem,
    InventoryRawScan,
    InventoryScanBatch,
    InventoryScanTerminal,
    InventorySession,
    InventorySessionManualConfirmation,
    InventorySessionManualQuantity,
)
from .services import import_inventory_scan_text, record_mobile_scan, record_terminal_raw_scan, start_inventory_session, _build_location_paths, ScanContext

INVENTORY_DETAIL_PAGE_SIZE = 200
INVENTORY_SHEET_FIRST_PAGE_ROWS = 24
INVENTORY_SHEET_CONTINUATION_ROWS = 42
INVENTORY_SHEET_SIGNATURE_ROWS_LIMIT = 28


class TerminalLoginView(LoginView):
    template_name = "inventory/terminal_login.html"
    redirect_authenticated_user = True

    def get_success_url(self):
        return self.get_redirect_url() or reverse("inventory:terminal-app")


def _visible_roots(roots, allowed, nodes):
    if allowed is None:
        return roots
    ancestors = set(allowed)
    pending = list(allowed)
    while pending:
        node = nodes.get(pending.pop())
        parent = node[1] if node else None
        if parent is not None and parent not in ancestors:
            ancestors.add(parent)
            pending.append(parent)
    return [root for root in roots if root.pk in ancestors]


class InventorySessionListView(LoginRequiredMixin, ListView):
    model = InventorySession
    template_name = "inventory/session_list.html"
    context_object_name = "sessions"

    def get_queryset(self):
        return get_visible_inventory_sessions(self.request.user, include_counts=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asset_type_labels = dict(get_inventory_asset_type_choices())
        nodes = {pk: (name, parent) for pk, name, parent in Location.objects.values_list('id', 'name', 'parent_id')}
        roots_by_session = {session.pk: list(session.scope_root_locations.all()) for session in context['sessions']}
        paths = _build_location_paths(nodes, {root.pk for roots in roots_by_session.values() for root in roots})
        allowed = get_accessible_location_ids(self.request.user)
        for session in context["sessions"]:
            roots = _visible_roots(roots_by_session[session.pk], allowed, nodes)
            session.root_locations_display = ", ".join(
                paths[location.pk] for location in roots
            )
            session.asset_type_scope_display = ", ".join(
                asset_type_labels.get(asset_type, asset_type)
                for asset_type in session.asset_type_scope
            )
        context["page_title"] = "Inwentaryzacje"
        return context


class InventorySessionStartView(LoginRequiredMixin, TemplateView):
    template_name = "inventory/session_start.html"

    def get(self, request, *args, **kwargs):
        return self.render_to_response(self.get_context_data(form=self._build_form()))

    def post(self, request, *args, **kwargs):
        form = self._build_form(data=request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        if self._can_configure_scope():
            root_locations = list(form.cleaned_data["root_locations"])
            asset_types = form.cleaned_data["asset_types"]
        else:
            root_locations = list(self._get_user_root_locations())
            asset_types = DEFAULT_ASSET_TYPES

        session = start_inventory_session(
            created_by=request.user,
            root_locations=root_locations,
            asset_types=asset_types,
        )
        messages.success(request, f"Utworzono sesję inwentaryzacji {session.number}.")
        return redirect("inventory:session-detail", pk=session.pk)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Rozpocznij inwentaryzację"
        context["can_configure_scope"] = self._can_configure_scope()
        context["user_root_locations"] = list(self._get_user_root_locations())
        return context

    def _build_form(self, data=None):
        if self._can_configure_scope():
            return InventorySessionStartForm(
                data,
                available_locations=self._get_configurable_root_locations(),
            )
        return SimpleInventorySessionStartForm(
            data,
            root_locations=self._get_user_root_locations(),
        )

    def _can_configure_scope(self):
        user = self.request.user
        if user.is_superuser:
            return True
        profile = getattr(user, "profile", None)
        return bool(profile and profile.role in {profile.Role.ADMIN, profile.Role.MANAGER})

    def _get_configurable_root_locations(self):
        accessible_location_ids = get_accessible_location_ids(self.request.user)
        if accessible_location_ids is None:
            return Location.objects.filter(parent__isnull=True).order_by("name", "id")
        if not accessible_location_ids:
            return Location.objects.none()
        return self.request.user.profile.allowed_locations.filter(id__in=accessible_location_ids).order_by("name", "id")

    def _get_user_root_locations(self):
        profile = getattr(self.request.user, "profile", None)
        if profile is None:
            return Location.objects.none()
        return profile.allowed_locations.order_by("name", "id")


class InventorySessionDetailView(LoginRequiredMixin, DetailView):
    model = InventorySession
    template_name = "inventory/session_detail.html"
    context_object_name = "session"

    def get_queryset(self):
        return get_visible_inventory_sessions(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.object
        analysis = _build_inventory_session_analysis(session, user=self.request.user)
        search_query = self.request.GET.get("q", "").strip()
        work_items = analysis["inventory_work_items"]
        if search_query:
            needle = search_query.casefold()
            work_items = [
                item for item in work_items
                if any(needle in (getattr(item["snapshot"], field) or "").casefold()
                       for field in ("barcode", "inventory_number", "name"))
            ]
        inventory_status = self.request.GET.get("status")
        if inventory_status not in {"all", "matching", "discrepancies"}:
            inventory_status = "discrepancies" if self.request.GET.get("discrepancies") == "1" else "all"
        if inventory_status == "discrepancies":
            work_items = [item for item in work_items if item["row_status"] != "matching"]
        elif inventory_status == "matching":
            work_items = [item for item in work_items if item["row_status"] == "matching"]
        sort_columns = [
            ("number", "Numer inwentarzowy", "number", lambda i: i["snapshot"].inventory_number),
            ("name", "Nazwa", "name", lambda i: i["snapshot"].name),
            ("type", "Rodzaj", "type", lambda i: i["snapshot"].asset_type_display),
            ("location", "Lokalizacja startowa", "start-location", lambda i: i["snapshot"].location_path),
            ("status", "Status spisu", "status", lambda i: i["row_status_label"]),
            ("scan_location", "Lokalizacja odczytu", "scan-location", lambda i: i["scanned_location_display"]),
            ("last_seen", "Ostatni odczyt", "last-seen", lambda i: i["last_seen_at"]),
            ("read", "Odczyt", "read", lambda i: i["read_quantity"]),
            ("barcode", "Kod kreskowy", "barcode", lambda i: i["scan_code_display"]),
            ("confirmed", "Potwierdź", "confirm", lambda i: i["manual_confirmed"]),
            ("manual", "Ręczne", "manual", lambda i: i["manual_quantity"]),
            ("actual", "Ilość faktyczna", "actual", lambda i: i["actual_quantity"]),
            ("expected", "Ilość oczekiwana", "record", lambda i: i["expected_quantity"]),
            ("difference", "Różnica", "difference", lambda i: i["difference"]),
        ]
        sort_key = self.request.GET.get("sort", "number")
        if sort_key not in {column[0] for column in sort_columns}:
            sort_key = "number"
        sort_direction = "desc" if self.request.GET.get("direction") == "desc" else "asc"
        value_for = next(column[3] for column in sort_columns if column[0] == sort_key)
        def normalized_value(item):
            value = value_for(item)
            return value.casefold() if isinstance(value, str) else value
        # Stable ties across pages; missing dates/values always follow known values.
        present = sorted((i for i in work_items if value_for(i) not in (None, "")),
                         key=lambda i: i["snapshot"].pk)
        missing = sorted((i for i in work_items if value_for(i) in (None, "")),
                         key=lambda i: i["snapshot"].pk)
        work_items = sorted(present, key=normalized_value, reverse=sort_direction == "desc") + missing
        headers = []
        for key, label, css, _ in sort_columns:
            params = self.request.GET.copy()
            params.pop("items_page", None)
            params.pop("discrepancies", None)
            params.update({"status": inventory_status, "sort": key,
                           "direction": "desc" if key == sort_key and sort_direction == "asc" else "asc"})
            headers.append({"label": label, "css": css, "url": "?" + params.urlencode(),
                            "active": key == sort_key,
                            "aria_sort": ("descending" if sort_direction == "desc" else "ascending") if key == sort_key else "none"})
        work_items_page = Paginator(
            work_items,
            INVENTORY_DETAIL_PAGE_SIZE,
        ).get_page(self.request.GET.get("items_page"))

        context.update(analysis)
        context["inventory_work_items"] = list(work_items_page.object_list)
        context["inventory_sort_headers"] = headers
        context["inventory_sort"] = sort_key
        context["inventory_direction"] = sort_direction
        context["inventory_status"] = inventory_status
        context["inventory_next_status"] = {"all": "matching", "matching": "discrepancies", "discrepancies": "all"}[inventory_status]
        context["inventory_search_query"] = search_query
        context["inventory_work_page"] = work_items_page
        context["inventory_work_page_size"] = INVENTORY_DETAIL_PAGE_SIZE
        context["page_title"] = session.number
        mobile_scan_path = reverse("inventory:mobile-scan", kwargs={"token": session.mobile_scan_token})
        context["mobile_scan_url"] = self.request.build_absolute_uri(mobile_scan_path) if context["can_manage_session"] else ""
        return context


class InventorySessionReportView(FeatureRequiredMixin, LoginRequiredMixin, DetailView):
    feature_key = "inventory_reports"
    model = InventorySession
    template_name = "inventory/session_report.html"
    context_object_name = "session"

    def get_queryset(self):
        return get_visible_inventory_sessions(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.object
        analysis = _build_inventory_session_analysis(session, user=self.request.user)
        inventory_work_items = analysis["inventory_work_items"]

        context.update(analysis)
        context["page_title"] = f"Raport {session.number}"
        context["no_read_items"] = [
            item for item in inventory_work_items
            if item["row_status"] == "no_read"
        ]
        context["wrong_location_items"] = [
            item for item in inventory_work_items
            if item["row_status"] == "wrong_location"
        ]
        context["quantity_difference_items"] = [
            item for item in inventory_work_items
            if item["uses_quantity_count"] and item["difference"] != 0
        ]
        context["unknown_code_items"] = [
            item for item in analysis["observed_items"]
            if item.status == InventoryObservedItem.Status.UNKNOWN_CODE
        ]
        context["manual_confirmation_items"] = [
            item for item in inventory_work_items
            if item["manual_confirmed"]
        ]
        return context


class InventorySessionDiscrepancyReportView(FeatureRequiredMixin, LoginRequiredMixin, DetailView):
    feature_key = "inventory_reports"
    model = InventorySession
    template_name = "inventory/session_discrepancy_report.html"
    context_object_name = "session"

    def get_queryset(self):
        return (
            get_visible_inventory_sessions(self.request.user)
            .filter(status=InventorySession.Status.CLOSED)

        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.object
        analysis = _build_inventory_session_analysis(session, user=self.request.user)
        inventory_work_items = analysis["inventory_work_items"]

        context.update(analysis)
        context["analysis"] = analysis
        context["summary"] = analysis
        context["page_title"] = f"Raport rozbieżności {session.number}"
        context["generated_at"] = timezone.now()
        context["missing_items"] = [
            item for item in inventory_work_items
            if item["actual_quantity"] == 0
        ]
        context["quantity_difference_items"] = [
            item for item in inventory_work_items
            if item["difference"] != 0
        ]
        context["wrong_location_items"] = [
            item for item in inventory_work_items
            if item["row_status"] == "wrong_location"
        ]
        context["unknown_code_items"] = [
            item for item in analysis["observed_items"]
            if item.status == InventoryObservedItem.Status.UNKNOWN_CODE
        ]
        context["manual_confirmation_items"] = [
            item for item in inventory_work_items
            if item["manual_confirmed"]
        ]
        return context


class InventorySessionSheetView(FeatureRequiredMixin, LoginRequiredMixin, DetailView):
    feature_key = "inventory_reports"
    model = InventorySession
    template_name = "inventory/session_sheet.html"
    context_object_name = "session"

    def get_queryset(self):
        return get_visible_inventory_sessions(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.object
        analysis = _build_inventory_session_analysis(session, user=self.request.user)

        context.update(analysis)
        context["page_title"] = f"Arkusz spisu {session.number}"
        context["sheet_generated_at"] = timezone.now()
        context["sheet_note"] = (
            "Arkusz końcowy — sesja zamknięta."
            if context["is_session_closed"]
            else "Arkusz roboczy — sesja nadal aktywna, dane mogą się zmieniać."
        )
        context["inventory_sheet_pages"] = _build_inventory_sheet_pages(
            context["inventory_work_items"],
        )
        return context


def _build_inventory_session_analysis(session, user=None):
        context = {}
        allowed = get_accessible_location_ids(user) if user is not None else None
        snapshots = session.snapshot_items.select_related('asset')
        observations = InventoryObservedItem.objects.filter(session=session)
        if allowed is not None:
            snapshots = snapshots.filter(location_fk_id_snapshot__in=allowed)
            observations = observations.filter(
                Q(asset_id__in=snapshots.values('asset_id_snapshot')) |
                Q(asset__isnull=True, scanned_location_id__in=allowed) |
                Q(asset__location_fk_id__in=allowed))
        asset_type_labels = dict(get_inventory_asset_type_choices())
        observed_items = list(
            observations
            .select_related("asset", "scanned_location")
            .order_by("-last_seen_at", "-id")
        )
        status_labels = {
            InventoryObservedItem.Status.FOUND_OK: "Zgodne",
            InventoryObservedItem.Status.FOUND_OTHER_LOCATION: "Inna lokalizacja",
            InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE: "Poza zakresem",
            InventoryObservedItem.Status.UNKNOWN_CODE: "Nieznany kod",
        }
        nodes = {pk: (name, parent) for pk, name, parent in
                 Location.objects.values_list("id", "name", "parent_id")}
        roots = _visible_roots(list(session.scope_root_locations.all()), allowed, nodes)
        paths = _build_location_paths(nodes, {item.scanned_location_id for item in observed_items
                                             if item.scanned_location_id} | {root.pk for root in roots})
        for item in observed_items:
            item.status_display_pl = status_labels.get(item.status, item.status)
            item.scanned_location_display = paths[item.scanned_location_id] if item.scanned_location_id else "-"

        is_quantity_based_by_type = dict(
            AssetTypeDictionary.objects.values_list("code", "is_quantity_based")
        )
        scanned_code_counts = _get_scanned_code_counts(session)
        observed_by_asset_id = {
            item.asset_id: item
            for item in observed_items
            if item.asset_id is not None
        }
        manual_quantities_by_asset_id = {
            item.asset_id: item.quantity
            for item in InventorySessionManualQuantity.objects.filter(session=session)
        }
        manually_confirmed_asset_ids = set(
            InventorySessionManualConfirmation.objects
            .filter(session=session)
            .values_list("asset_id", flat=True)
        )
        snapshot_items = list(snapshots)
        # A historical accounting parent outside the visible snapshot scope must
        # not disclose its number through a component's inherited identifier.
        from copy import copy
        visible_parents = {item.asset_id_snapshot for item in snapshot_items if item.collection_snapshot.get('role') == 'parent'}
        for index, item in enumerate(snapshot_items):
            if item.collection_snapshot.get('role') == 'member' and item.collection_snapshot.get('parent_id') not in visible_parents:
                item = copy(item)
                item.inventory_number = '—'
                snapshot_items[index] = item
        inventory_work_items = []
        for snapshot_item in snapshot_items:
            observed_item = observed_by_asset_id.get(snapshot_item.asset_id_snapshot)
            if observed_item is None:
                display_status = "Brak odczytu"
                scanned_location_display = "-"
                last_seen_at = None
            else:
                display_status = status_labels.get(observed_item.status, observed_item.status)
                scanned_location_display = observed_item.scanned_location_display
                last_seen_at = observed_item.last_seen_at
            is_quantity_based = is_quantity_based_by_type.get(snapshot_item.asset_type, False)
            expected_quantity = _get_snapshot_expected_quantity(snapshot_item)
            uses_quantity_count = _uses_quantity_count(
                snapshot_item=snapshot_item,
                is_quantity_based=is_quantity_based,
                expected_quantity=expected_quantity,
            )
            scan_code_display = snapshot_item.barcode or snapshot_item.inventory_number
            read_quantity = _get_inventory_read_quantity(
                snapshot_item=snapshot_item,
                observed_item=observed_item,
                is_quantity_based=is_quantity_based,
                scanned_code_counts=scanned_code_counts,
            )
            manual_quantity = manual_quantities_by_asset_id.get(snapshot_item.asset_id_snapshot, 0)
            if not uses_quantity_count:
                manual_quantity = 0
            manual_confirmed = (
                not uses_quantity_count
                and snapshot_item.asset_id_snapshot in manually_confirmed_asset_ids
            )
            actual_quantity = _get_inventory_actual_quantity(
                read_quantity=read_quantity,
                manual_quantity=manual_quantity,
                manual_confirmed=manual_confirmed,
                uses_quantity_count=uses_quantity_count,
            )
            purchase_value = _get_snapshot_purchase_value(snapshot_item)
            sheet_total_value = purchase_value * actual_quantity if purchase_value is not None else None
            difference = actual_quantity - expected_quantity
            work_item = {
                "snapshot": snapshot_item,
                "observed": observed_item,
                "display_status": display_status,
                "scanned_location_display": scanned_location_display,
                "last_seen_at": last_seen_at,
                "is_quantity_based": is_quantity_based,
                "uses_quantity_count": uses_quantity_count,
                "read_quantity": read_quantity,
                "manual_quantity": manual_quantity,
                "manual_confirmed": manual_confirmed,
                "actual_quantity": actual_quantity,
                "expected_quantity": expected_quantity,
                "purchase_value": purchase_value,
                "purchase_value_display": _format_inventory_purchase_value(purchase_value),
                "sheet_unit_label": "szt.",
                "sheet_unit_price_display": _format_inventory_sheet_money(purchase_value),
                "sheet_total_value_display": _format_inventory_sheet_money(sheet_total_value),
                "difference": difference,
                "difference_display": _format_inventory_difference(difference),
                "scan_code_display": scan_code_display,
            }
            row_status = _get_inventory_row_status(work_item)
            work_item.update(
                {
                    "row_status": row_status["code"],
                    "row_status_label": row_status["label"],
                    "row_status_variant": row_status["variant"],
                }
            )
            inventory_work_items.append(work_item)

        from .collections import split_inventory_collections
        inventory_work_items, parent_items, collection_summaries = split_inventory_collections(inventory_work_items)
        context['collection_parent_items'] = parent_items
        context['collection_summaries'] = collection_summaries
        context["page_title"] = session.number
        context["root_locations_display"] = ", ".join(
            paths[location.pk] for location in roots
        )
        context["asset_type_scope_display"] = ", ".join(
            asset_type_labels.get(asset_type, asset_type)
            for asset_type in session.asset_type_scope
        )
        context["is_session_closed"] = session.status == InventorySession.Status.CLOSED
        context["session_status_display_pl"] = (
            "Zamknięta" if context["is_session_closed"] else "Aktywna"
        )
        inventory_summary = _get_inventory_summary(
            inventory_work_items=inventory_work_items,
            observed_items=observed_items,
        )
        context["snapshot_items"] = snapshot_items
        context.update(inventory_summary)
        context["observed_assets_count"] = inventory_summary["read_count"]
        context["found_ok_count"] = inventory_summary["matching_count"]
        context["found_other_location_count"] = inventory_summary["wrong_location_count"]
        context["found_out_of_scope_count"] = sum(
            1 for item in observed_items if item.status == InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE
        )
        context["observed_items"] = observed_items
        context["inventory_work_items"] = inventory_work_items
        context["problem_items"] = [
            item for item in observed_items
            if item.status in {
                InventoryObservedItem.Status.UNKNOWN_CODE,
                InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE,
            }
        ]
        context["scan_batches"] = (
            InventoryScanBatch.objects
            .filter(session=session)
            .order_by("-created_at", "-id")[:10]
        )
        full_scope = user is None or _can_manage_inventory_session(user, session)
        context['can_manage_session'] = full_scope
        if not full_scope:
            context['scan_batches'] = InventoryScanBatch.objects.none()
        context["terminal_summaries"] = _get_terminal_summaries(session) if full_scope else []
        return context


def _can_manage_inventory_session(user, session):
    allowed = get_accessible_location_ids(user)
    if allowed is None:
        return True
    required = _get_location_subtree_ids(session.scope_root_locations.all())
    required.update(session.snapshot_items.values_list('location_fk_id_snapshot', flat=True))
    return bool(required) and required.issubset(allowed)


class InventorySessionCloseView(LoginRequiredMixin, View):
    @method_decorator(transaction.atomic)
    def post(self, request, pk):
        session = get_object_or_404(get_visible_inventory_sessions(request.user).select_for_update(of=('self',)), pk=pk)
        if not _can_manage_inventory_session(request.user, session):
            raise PermissionDenied
        if session.status == InventorySession.Status.ACTIVE:
            session.status = InventorySession.Status.CLOSED
            session.closed_at = timezone.now()
            session.save(update_fields=['status', 'closed_at', 'updated_at'])
            messages.success(request, f'Zamknięto sesję inwentaryzacji {session.number}.')
        return redirect('inventory:session-detail', pk=session.pk)


@login_required
@require_POST
def apply_inventory_session_to_assets(request, pk):
    require_feature(request, "inventory_reports")
    with transaction.atomic():
        session = get_object_or_404(
            get_visible_inventory_sessions(request.user).select_for_update(of=("self",)),
            pk=pk,
        )

        if not _can_manage_inventory_session(request.user, session):
            raise PermissionDenied

        if session.status != InventorySession.Status.CLOSED:
            messages.error(request, "Wyniki można nanieść na Ewidencję tylko dla zamkniętej sesji.")
            return redirect("inventory:session-detail", pk=session.pk)

        if session.applied_to_assets_at is not None:
            messages.info(request, "Wyniki tej sesji zostały już naniesione na Ewidencję.")
            return redirect("inventory:session-detail", pk=session.pk)

        from .collections import validate_inventory_collection_versions
        from django.core.exceptions import ValidationError
        try:
            validate_inventory_collection_versions(session)
        except ValidationError as error:
            messages.error(request, '; '.join(error.messages))
            return redirect('inventory:session-detail', pk=session.pk)
        analysis = _build_inventory_session_analysis(session, user=request.user)
        if any(summary['partial'] for summary in analysis['collection_summaries']):
            messages.error(request, 'Niepełny zakres zestawu. Rozliczenie wymaga uzgodnienia; nie naniesiono zmian.')
            return redirect('inventory:session-detail', pk=session.pk)
        for summary in analysis['collection_summaries']:
            parent = summary['parent_item']
            if parent is not None:
                parent['actual_quantity'] = int(summary['complete'])
        apply_items = analysis['inventory_work_items'] + analysis['collection_parent_items']
        now = timezone.now()
        from .models import InventorySnapshotItem
        # Serialize with withdrawals; old inventory results must not resurrect quantities.
        locked_assets = {asset.pk: asset for asset in Asset.objects.select_for_update().filter(
            pk__in=InventorySnapshotItem.objects.filter(session=session).values('asset_id')
        ).order_by('pk')}
        try:
            # Recheck after asset locks: a set could have been created while a
            # legacy closed session was waiting for these asset rows.
            validate_inventory_collection_versions(session, lock=False)
        except ValidationError as error:
            messages.error(request, '; '.join(error.messages))
            return redirect('inventory:session-detail', pk=session.pk)
        from assets.models import AssetWithdrawal
        from .models import InventorySnapshotItem
        if AssetWithdrawal.objects.filter(asset_id__in=locked_assets, is_historical=False,
                registered_at__gte=session.started_at).exists():
            messages.error(request, "Po rozpoczęciu spisu zarejestrowano wycofanie środka. Wyniki wymagają uzgodnienia; nie naniesiono zmian.")
            return redirect("inventory:session-detail", pk=session.pk)
        for work_item in apply_items:
            asset = locked_assets.get(work_item['snapshot'].asset_id)
            snapshot = work_item['snapshot']
            if asset is not None and (not asset.is_active or
                    asset.current_quantity != snapshot.collection_snapshot.get('current_quantity_at_start', work_item['expected_quantity']) or
                    (asset.last_inventory_at is not None and asset.last_inventory_at > session.started_at
                     and asset.last_inventory_session_id != session.pk)):
                messages.error(request, 'Po rozpoczęciu spisu zmienił się stan lub ilość środka. Wymagane uzgodnienie; nie naniesiono zmian.')
                return redirect('inventory:session-detail', pk=session.pk)
        assets_to_update = []
        history_entries = []
        for work_item in apply_items:
            asset = locked_assets.get(work_item["snapshot"].asset_id)
            if asset is None:
                continue
            if not asset.is_active:
                continue
            old_current_quantity = asset.current_quantity
            is_set_parent = work_item['snapshot'].collection_snapshot.get('role') == 'parent'
            new_current_quantity = old_current_quantity if is_set_parent else work_item["actual_quantity"]
            if old_current_quantity != new_current_quantity:
                history_entries.append(
                    AssetHistoryEntry(
                        asset=asset,
                        occurred_at=now,
                        operator=request.user,
                        event_type=AssetHistoryEntry.EventType.INVENTORY_APPLIED,
                        description="Naniesiono wynik inwentaryzacji",
                        old_value=str(old_current_quantity),
                        new_value=str(new_current_quantity),
                        field_name="current_quantity",
                        source_object_type="InventorySession",
                        source_object_id=session.pk,
                    )
                )
            asset.current_quantity = new_current_quantity
            asset.last_inventory_quantity = work_item["actual_quantity"]
            asset.last_inventory_session = session
            asset.last_inventory_at = now
            asset.last_inventory_date = timezone.localdate(now)
            asset.updated_at = now
            assets_to_update.append(asset)

        Asset.objects.bulk_update(
            assets_to_update,
            [
                "current_quantity",
                "last_inventory_quantity",
                "last_inventory_session",
                "last_inventory_at",
                "last_inventory_date",
                "updated_at",
            ],
            batch_size=500,
        )
        AssetHistoryEntry.objects.bulk_create(history_entries, batch_size=500)

        session.applied_to_assets_at = now
        session.applied_to_assets_by = request.user
        session.save(update_fields=["applied_to_assets_at", "applied_to_assets_by", "updated_at"])

    messages.success(
        request,
        f"Naniesiono wyniki inwentaryzacji na {len(assets_to_update)} składników Ewidencji.",
    )
    return redirect("inventory:session-detail", pk=session.pk)


def _get_scanned_code_counts(session, codes=None):
    code_counts = defaultdict(int)
    wanted = set(codes) if codes is not None else None
    locations = ScanCode.objects.filter(location_id__isnull=False)
    if wanted is not None:
        locations = locations.filter(code__in=wanted)
    location_codes = set(locations.values_list('code', flat=True))
    for raw_text in InventoryScanBatch.objects.filter(session=session).values_list('raw_text', flat=True).iterator():
        lines = [line.strip() for line in str(raw_text).splitlines() if line.strip()]
        for code in lines[1:]:
            if code not in location_codes and (wanted is None or code in wanted):
                code_counts[code] += 1
    raw = InventoryRawScan.objects.filter(session=session).exclude(processing_status__in=[
        InventoryRawScan.ProcessingStatus.LOCATION, InventoryRawScan.ProcessingStatus.ERROR])
    if wanted is not None:
        raw = raw.filter(code__in=wanted)
    for row in raw.order_by().values('code').annotate(total=Count('pk')):
        if row['code'] and row['code'] not in location_codes:
            code_counts[row['code']] += row['total']
    return code_counts


def _get_terminal_summaries(session):
    terminals = InventoryScanTerminal.objects.filter(session=session).annotate(
        raw_scan_count=Count('raw_scans'), last_scan=Max('raw_scans__scanned_at'),
        unknown=Count('raw_scans', filter=Q(raw_scans__processing_result__type='unknown'))
    ).order_by('name', 'id')
    return [{'terminal': terminal, 'raw_scan_count': terminal.raw_scan_count,
             'last_scan_at': terminal.last_scan, 'unknown_count': terminal.unknown}
            for terminal in terminals]


def _get_snapshot_expected_quantity(snapshot_item):
    if snapshot_item.record_quantity_snapshot is not None:
        return snapshot_item.record_quantity_snapshot
    if snapshot_item.asset_id and snapshot_item.asset:
        return snapshot_item.asset.current_quantity
    return 1


def _get_snapshot_purchase_value(snapshot_item):
    if snapshot_item.purchase_value_snapshot is not None:
        return snapshot_item.purchase_value_snapshot
    if snapshot_item.asset_id and snapshot_item.asset:
        return snapshot_item.asset.purchase_value
    return None


def _format_inventory_purchase_value(value):
    return f"{value} zł" if value is not None else "-"


def _format_inventory_sheet_money(value):
    return f"{value:.2f}" if value is not None else ""


def _build_inventory_sheet_pages(inventory_work_items):
    numbered_items = []
    for index, item in enumerate(inventory_work_items, start=1):
        sheet_item = item.copy()
        sheet_item["sheet_row_number"] = index
        numbered_items.append(sheet_item)
    if not numbered_items:
        return [{"items": [], "show_summary": True}]

    pages = [{"items": numbered_items[:INVENTORY_SHEET_FIRST_PAGE_ROWS], "show_summary": False}]
    remaining_items = numbered_items[INVENTORY_SHEET_FIRST_PAGE_ROWS:]
    for index in range(0, len(remaining_items), INVENTORY_SHEET_CONTINUATION_ROWS):
        pages.append({
            "items": remaining_items[index:index + INVENTORY_SHEET_CONTINUATION_ROWS],
            "show_summary": False,
        })

    if len(pages[-1]["items"]) > INVENTORY_SHEET_SIGNATURE_ROWS_LIMIT:
        pages.append({"items": [], "show_summary": True})
    else:
        pages[-1]["show_summary"] = True
    return pages


def _get_inventory_read_quantity(*, snapshot_item, observed_item, is_quantity_based, scanned_code_counts):
    if not is_quantity_based:
        return 1 if observed_item is not None else 0

    return scanned_code_counts.get(snapshot_item.barcode, 0) if snapshot_item.barcode else 0



def _uses_quantity_count(*, snapshot_item, is_quantity_based, expected_quantity=None):
    if is_quantity_based:
        return True
    if expected_quantity is None:
        expected_quantity = _get_snapshot_expected_quantity(snapshot_item)
    return expected_quantity > 1


def _get_inventory_actual_quantity(*, read_quantity, manual_quantity, manual_confirmed, uses_quantity_count):
    if uses_quantity_count:
        return read_quantity + manual_quantity
    return 1 if read_quantity == 1 or manual_confirmed else 0


def _format_inventory_difference(difference):
    return f"+{difference}" if difference > 0 else str(difference)


def _get_inventory_row_status(work_item):
    observed_item = work_item.get("observed")
    if (
        observed_item is not None
        and observed_item.status == InventoryObservedItem.Status.FOUND_OTHER_LOCATION
    ):
        return {"code": "wrong_location", "label": "Inna lokalizacja", "variant": "warning"}
    if work_item["actual_quantity"] == 0:
        return {"code": "no_read", "label": "Brak odczytu", "variant": "neutral"}
    if work_item["difference"] < 0:
        return {"code": "shortage", "label": "Niedobór", "variant": "problem"}
    if work_item["difference"] > 0:
        return {"code": "surplus", "label": "Nadwyżka", "variant": "warning"}
    return {"code": "matching", "label": "Zgodne", "variant": "ok"}


def _get_inventory_summary(*, inventory_work_items, observed_items):
    return {
        "snapshot_total": len(inventory_work_items),
        "read_count": sum(1 for item in inventory_work_items if item["actual_quantity"] > 0),
        "matching_count": sum(1 for item in inventory_work_items if item["difference"] == 0),
        "shortage_count": sum(1 for item in inventory_work_items if item["difference"] < 0),
        "surplus_count": sum(1 for item in inventory_work_items if item["difference"] > 0),
        "no_read_count": sum(1 for item in inventory_work_items if item["actual_quantity"] == 0),
        "wrong_location_count": sum(
            1
            for item in inventory_work_items
            if item["observed"] is not None
            and item["observed"].status == InventoryObservedItem.Status.FOUND_OTHER_LOCATION
        ),
        "unknown_code_count": sum(
            1 for item in observed_items if item.status == InventoryObservedItem.Status.UNKNOWN_CODE
        ),
        "manual_confirmation_count": sum(1 for item in inventory_work_items if item["manual_confirmed"]),
        "quantity_difference_count": sum(
            1 for item in inventory_work_items if item["uses_quantity_count"] and item["difference"] != 0
        ),
    }


def _closed_inventory_session_response():
    return JsonResponse({"ok": False, "error": "Sesja jest zamknięta."}, status=409)


@login_required
@require_POST
@transaction.atomic
def manual_quantity_api(request, session_id):
    session = get_object_or_404(get_visible_inventory_sessions(request.user).select_for_update(of=("self",)), pk=session_id)
    if session.status == InventorySession.Status.CLOSED:
        return _closed_inventory_session_response()

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    asset_id = _parse_positive_int(payload.get("asset_id"), default=0)
    if asset_id <= 0:
        return JsonResponse({"ok": False, "error": "asset_id is required."}, status=400)

    raw_quantity = payload.get("quantity", 0)
    if raw_quantity == "":
        quantity = 0
    else:
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "quantity must be an integer."}, status=400)
    if quantity < 0:
        return JsonResponse({"ok": False, "error": "quantity must be non-negative."}, status=400)

    snapshot_item = get_object_or_404(
        session.snapshot_items.select_related("asset"),
        asset_id_snapshot=asset_id,
    )
    if snapshot_item.collection_snapshot.get('role') == 'parent':
        return JsonResponse({'ok': False, 'error': 'Potwierdź elementy zestawu osobno; środek nadrzędny jest rozliczany zbiorczo.'}, status=400)
    if snapshot_item.asset_id is None:
        return JsonResponse({"ok": False, "error": "asset_id does not point to an active asset."}, status=404)

    allowed = get_accessible_location_ids(request.user)
    if allowed is not None and snapshot_item.location_fk_id_snapshot not in allowed:
        raise PermissionDenied
    is_quantity_based = _is_snapshot_item_quantity_based(snapshot_item)
    uses_quantity_count = _uses_quantity_count(
        snapshot_item=snapshot_item,
        is_quantity_based=is_quantity_based,
    )
    if not uses_quantity_count:
        return JsonResponse({"ok": False, "error": "Manual quantity is only available for quantity-counted assets."}, status=400)

    manual_quantity, _created = InventorySessionManualQuantity.objects.update_or_create(
        session=session,
        asset=snapshot_item.asset,
        defaults={
            "quantity": quantity,
            "updated_by": request.user,
        },
    )

    observed_item = InventoryObservedItem.objects.filter(session=session, asset_id=asset_id).first()
    read_quantity = _get_inventory_read_quantity(
        snapshot_item=snapshot_item,
        observed_item=observed_item,
        is_quantity_based=is_quantity_based,
        scanned_code_counts=_get_scanned_code_counts(session, {snapshot_item.barcode}),
    )

    return JsonResponse(
        {
            "ok": True,
            "asset_id": asset_id,
            "manual_quantity": manual_quantity.quantity,
            "actual_quantity": _get_inventory_actual_quantity(
                read_quantity=read_quantity,
                manual_quantity=manual_quantity.quantity,
                manual_confirmed=False,
                uses_quantity_count=True,
            ),
        }
    )


@login_required
@require_POST
@transaction.atomic
def manual_confirmation_api(request, session_id):
    session = get_object_or_404(get_visible_inventory_sessions(request.user).select_for_update(of=("self",)), pk=session_id)
    if session.status == InventorySession.Status.CLOSED:
        return _closed_inventory_session_response()

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)

    asset_id = _parse_positive_int(payload.get("asset_id"), default=0)
    if asset_id <= 0:
        return JsonResponse({"ok": False, "error": "asset_id is required."}, status=400)

    if "confirmed" not in payload or not isinstance(payload.get("confirmed"), bool):
        return JsonResponse({"ok": False, "error": "confirmed must be a boolean."}, status=400)
    confirmed = payload["confirmed"]

    snapshot_item = get_object_or_404(
        session.snapshot_items.select_related("asset"),
        asset_id_snapshot=asset_id,
    )
    if snapshot_item.collection_snapshot.get('role') == 'parent':
        return JsonResponse({'ok': False, 'error': 'Potwierdź elementy zestawu osobno; środek nadrzędny jest rozliczany zbiorczo.'}, status=400)
    if snapshot_item.asset_id is None:
        return JsonResponse({"ok": False, "error": "asset_id does not point to an active asset."}, status=404)

    is_quantity_based = _is_snapshot_item_quantity_based(snapshot_item)
    uses_quantity_count = _uses_quantity_count(
        snapshot_item=snapshot_item,
        is_quantity_based=is_quantity_based,
    )
    if uses_quantity_count:
        return JsonResponse({"ok": False, "error": "Manual confirmation is only available for single regular assets."}, status=400)

    allowed = get_accessible_location_ids(request.user)
    if allowed is not None and snapshot_item.location_fk_id_snapshot not in allowed:
        raise PermissionDenied
    if confirmed:
        InventorySessionManualConfirmation.objects.update_or_create(
            session=session,
            asset=snapshot_item.asset,
            defaults={"confirmed_by": request.user},
        )
    else:
        InventorySessionManualConfirmation.objects.filter(
            session=session,
            asset=snapshot_item.asset,
        ).delete()

    observed_item = InventoryObservedItem.objects.filter(session=session, asset_id=asset_id).first()
    read_quantity = _get_inventory_read_quantity(
        snapshot_item=snapshot_item,
        observed_item=observed_item,
        is_quantity_based=False,
        scanned_code_counts={},
    )

    return JsonResponse(
        {
            "ok": True,
            "asset_id": asset_id,
            "manual_confirmed": confirmed,
            "actual_quantity": _get_inventory_actual_quantity(
                read_quantity=read_quantity,
                manual_quantity=0,
                manual_confirmed=confirmed,
                uses_quantity_count=False,
            ),
        }
    )


def _is_snapshot_item_quantity_based(snapshot_item):
    return AssetTypeDictionary.objects.filter(
        code=snapshot_item.asset_type,
        is_quantity_based=True,
    ).exists()


def _parse_positive_int(raw_value, default):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@login_required
def session_stats_api(request, pk):
    session = get_object_or_404(get_visible_inventory_sessions(request.user), pk=pk)
    from .sql_analysis import inventory_summary
    analysis = inventory_summary(session, request.user)
    if analysis is None:
        analysis = _build_inventory_session_analysis(session, user=request.user)
    stat_keys = [
        "snapshot_total", "read_count", "matching_count", "shortage_count", "surplus_count",
        "no_read_count", "wrong_location_count", "found_out_of_scope_count", "unknown_code_count",
        "manual_confirmation_count", "quantity_difference_count",
    ]
    summary = {key: analysis.get(key, 0) for key in stat_keys}
    summary["problem_count"] = (
        summary["wrong_location_count"]
        + summary["found_out_of_scope_count"]
        + summary["unknown_code_count"]
    )
    return JsonResponse({"ok": True, "summary": summary})


@ensure_csrf_cookie
@login_required(login_url="inventory:terminal-login")
def terminal_app_view(request):
    return render(
        request,
        "inventory/terminal_app.html",
        {
            "page_title": "Terminal inwentaryzacyjny",
            "terminal_data": _terminal_config(),
        },
    )


@login_required
@require_GET
def terminal_sessions_api(request):
    sessions = (
        get_visible_inventory_sessions(request.user, include_counts=True)
        .filter(status=InventorySession.Status.ACTIVE)
        .order_by("-started_at", "-id")
    )
    sessions = list(sessions)
    nodes = {pk: (name,parent) for pk,name,parent in Location.objects.values_list('id','name','parent_id')}
    allowed = get_accessible_location_ids(request.user)
    roots = {session.pk: _visible_roots(list(session.scope_root_locations.all()), allowed, nodes) for session in sessions}
    paths = _build_location_paths(nodes, {root.pk for roots in roots.values() for root in roots})
    return JsonResponse(
        {
            "ok": True,
            "sessions": [
                {
                    "id": session.id,
                    "number": session.number,
                    "started_at": session.started_at.isoformat(),
                    "root_locations": ", ".join(paths[root.pk] for root in roots[session.pk]),
                    "snapshot_items_count": session.snapshot_items_count,
                }
                for session in sessions
            ],
        }
    )


@login_required
@require_GET
def terminal_session_bootstrap_api(request, session_id):
    session = get_object_or_404(
        get_visible_inventory_sessions(request.user).filter(status=InventorySession.Status.ACTIVE),
        pk=session_id,
    )
    locations = _scan_location_catalog(get_accessible_location_ids(request.user))
    terminals = [
        {
            "id": terminal.id,
            "name": terminal.name,
            "description": terminal.description,
            "last_seen_at": terminal.last_seen_at.isoformat() if terminal.last_seen_at else None,
        }
        for terminal in session.scan_terminals.filter(is_active=True).order_by("name", "id")
    ]
    return JsonResponse(
        {
            "ok": True,
            "session": {
                "id": session.id,
                "number": session.number,
                "status": session.status,
            },
            "locations": locations,
            "terminals": terminals,
        }
    )


@login_required
@require_POST
def terminal_create_api(request, session_id):
    session = get_object_or_404(
        get_visible_inventory_sessions(request.user).filter(status=InventorySession.Status.ACTIVE),
        pk=session_id,
    )
    payload, error_response = _get_json_payload(request)
    if error_response is not None:
        return error_response

    name = str(payload.get("name") or "").strip()
    if name.startswith('Telefon — skaner mobilny') or len(name) > 80:
        return JsonResponse({'ok': False, 'error': 'Niedozwolona nazwa terminala.'}, status=400)
    if not name:
        return JsonResponse({"ok": False, "error": "Nazwa terminala jest wymagana."}, status=400)
    description = str(payload.get("description") or "").strip()

    terminal, _created = InventoryScanTerminal.objects.get_or_create(
        session=session,
        name=name,
        defaults={
            "description": description,
            "created_by": request.user,
        },
    )
    if description and terminal.description != description:
        terminal.description = description
        terminal.save(update_fields=["description", "updated_at"])

    return JsonResponse(
        {
            "ok": True,
            "terminal": {
                "id": terminal.id,
                "name": terminal.name,
                "description": terminal.description,
            },
        }
    )


@login_required
@require_POST
def terminal_sync_api(request, session_id, terminal_id):
    session = get_object_or_404(
        get_visible_inventory_sessions(request.user),
        pk=session_id,
    )
    if session.status == InventorySession.Status.CLOSED:
        return _closed_inventory_session_response()

    terminal = get_object_or_404(
        InventoryScanTerminal.objects.filter(session=session, is_active=True, source='terminal'),
        pk=terminal_id,
    )
    payload, error_response = _get_json_payload(request)
    if error_response is not None:
        return error_response

    scans = payload.get("scans")
    if not isinstance(scans, list):
        return JsonResponse({"ok": False, "error": "scans must be a list."}, status=400)

    if len(scans) > 100:
        return JsonResponse({'ok': False, 'error': 'Paczka może zawierać najwyżej 100 skanów.'}, status=400)
    accepted = []
    errors = []
    allowed_location_ids = get_accessible_location_ids(request.user)
    codes = {str(value) for scan in scans if isinstance(scan, dict)
             for value in (scan.get('code') or '', scan.get('current_location_code') or '')
             if len(str(value)) <= 120}
    scan_context = ScanContext(session, codes)
    for index, scan in enumerate(scans):
        if not isinstance(scan, dict):
            errors.append({"index": index, "error": "scan must be an object."})
            continue
        client_scan_id = str(scan.get("client_scan_id") or "").strip()
        code = str(scan.get("code") or "").strip()
        if len(client_scan_id) > 64 or len(code) > 120 or len(str(scan.get('current_location_code') or '')) > 120:
            errors.append({'index': index, 'error': 'Kod lub identyfikator jest zbyt długi.'})
            continue
        if not client_scan_id or not code:
            errors.append({"index": index, "error": "client_scan_id and code are required."})
            continue
        scanned_at = _parse_client_datetime(scan.get("scanned_at"))
        try:
            raw_scan, created = record_terminal_raw_scan(
                session=session,
                terminal=terminal,
                client_scan_id=client_scan_id,
                code=code,
                scanned_at=scanned_at,
                current_location_code=scan.get("current_location_code") or None,
                allowed_location_ids=allowed_location_ids,
                update_terminal=False,
                scan_context=scan_context,
            )
        except ValueError as error:
            errors.append({'index': index, 'error': str(error)})
            continue
        accepted.append(
            {
                "client_scan_id": client_scan_id,
                "raw_scan_id": raw_scan.id,
                "created": created,
                "result": raw_scan.processing_result,
            }
        )

    if accepted:
        InventoryScanTerminal.objects.filter(pk=terminal.pk).update(last_seen_at=timezone.now(), updated_at=timezone.now())
    return JsonResponse(
        {
            "ok": not errors,
            "accepted": accepted,
            "errors": errors,
            "server_scan_count": InventoryRawScan.objects.filter(terminal=terminal).count(),
        },
        status=207 if errors and accepted else (400 if errors else 200),
    )


def _get_json_payload(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}"), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, JsonResponse({"ok": False, "error": "Invalid JSON body."}, status=400)


def _parse_client_datetime(value):
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return timezone.now()
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


@login_required
@require_POST
def scan_file_import_api(request):
    import_user = request.user

    try:
        raw_text = request.body.decode("utf-8")
    except UnicodeDecodeError:
        return JsonResponse({"status": "error", "message": "Request body must be valid UTF-8."}, status=400)

    if not raw_text.strip():
        return JsonResponse({"status": "error", "message": "Request body is empty."}, status=400)

    session_number = _get_session_number_from_scan_text(raw_text)
    session = InventorySession.objects.filter(number=session_number).first()
    if session is None:
        return JsonResponse({"status": "error", "message": "Inventory session does not exist."}, status=400)
    if not get_visible_inventory_sessions(import_user).filter(pk=session.pk).exists():
        return JsonResponse(
            {"status": "error", "message": "Brak dostępu do tej sesji inwentaryzacji."},
            status=403,
        )

    if not _can_manage_inventory_session(import_user, session):
        raise PermissionDenied
    if session.status == InventorySession.Status.CLOSED:
        return JsonResponse(
            {"ok": False, "status": "error", "error": "Sesja jest zamknięta.", "message": "Sesja jest zamknięta."},
            status=409,
        )

    try:
        batch = import_inventory_scan_text(raw_text, uploaded_by=import_user,
                                           allowed_location_ids=get_accessible_location_ids(import_user))
    except ValueError as exc:
        return JsonResponse({"status": "error", "message": str(exc)}, status=400)

    return JsonResponse(
        {
            "status": "ok",
            "session": batch.session.number,
            "batch_id": batch.id,
            "total_lines": batch.total_lines,
            "processed_lines": batch.processed_lines,
            "recognized_assets_count": batch.recognized_assets_count,
            "unknown_codes_count": batch.unknown_codes_count,
        }
    )


def _get_session_number_from_scan_text(raw_text: str) -> str:
    for line in raw_text.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def get_visible_inventory_sessions(user, *, include_counts=False):
    queryset = InventorySession.objects.select_related('created_by').prefetch_related('scope_root_locations')
    allowed = get_accessible_location_ids(user)
    if allowed is not None:
        if not allowed:
            return queryset.none()
        parents = dict(Location.objects.values_list('id', 'parent_id'))
        ancestors = set(allowed)
        pending = list(allowed)
        while pending:
            parent = parents.get(pending.pop())
            if parent is not None and parent not in ancestors:
                ancestors.add(parent)
                pending.append(parent)
        queryset = queryset.filter(pk__in=InventorySession.objects.filter(scope_root_locations__in=ancestors).values('pk'))
    if include_counts:
        queryset = queryset.annotate(snapshot_items_count=Count('snapshot_items', filter=(Q() if allowed is None else Q(snapshot_items__location_fk_id_snapshot__in=allowed))))
    return queryset.order_by('-started_at', '-id')


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


def _scan_location_catalog(allowed_location_ids):
    queryset = Location.objects.filter(is_active=True).order_by('code', 'id')
    if allowed_location_ids is not None:
        queryset = queryset.filter(pk__in=allowed_location_ids)
    ambiguous = set(ScanCode.objects.filter(asset_id__isnull=False, location_id__isnull=False).values_list('code', flat=True))
    nodes = {row['id']: row for row in Location.objects.values('id', 'name', 'parent_id')}
    def location_path(location):
        names, seen, node_id = [], set(), location.pk
        while node_id in nodes and node_id not in seen:
            seen.add(node_id)
            names.append(nodes[node_id]['name'])
            node_id = nodes[node_id]['parent_id']
        return ' / '.join(reversed(names))
    return [{'code': location.code, 'name': location.name, 'path': location_path(location),
             'ambiguous': location.code in ambiguous} for location in queryset]


def mobile_scan_view(request, token):
    session = get_object_or_404(InventorySession, mobile_scan_token=token)
    locations = _scan_location_catalog(_get_location_subtree_ids(session.scope_root_locations.all()))

    scan_count = InventoryObservedItem.objects.filter(session=session).count()
    context = {
        "session": session,
        "session_data": {
            "session_number": session.number,
            "session_status": session.status,
            "scan_count": scan_count,
            "token": token,
            "api_url": reverse("inventory:mobile-scan-api", kwargs={"token": token}),
            "locations": locations,
        },
    }
    return render(request, "inventory/mobile_scan.html", context)


@csrf_exempt
@require_POST
def mobile_scan_api(request, token):
    session = get_object_or_404(InventorySession, mobile_scan_token=token)
    if session.status != InventorySession.Status.ACTIVE:
        return JsonResponse({"ok": False, "error": "Session is closed."}, status=400)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)

    code = str(data.get("code", "")).strip()
    if not code:
        return JsonResponse({"ok": False, "error": "Missing code."}, status=400)

    import uuid
    client_scan_id = str(data.get('client_scan_id') or uuid.uuid4()).strip()
    if len(code) > 120 or len(client_scan_id) > 64:
        return JsonResponse({'ok': False, 'error': 'Kod lub identyfikator jest zbyt długi.'}, status=400)
    current_location_code = str(data.get('current_location_code') or '')
    if len(current_location_code) > 120:
        return JsonResponse({'ok': False, 'error': 'Kod lokalizacji jest zbyt długi.'}, status=400)
    with transaction.atomic():
        session = InventorySession.objects.select_for_update().get(pk=session.pk)
        if session.status != InventorySession.Status.ACTIVE:
            return _closed_inventory_session_response()
        terminal, _ = InventoryScanTerminal.objects.get_or_create(
            session=session, source='phone', defaults={'name': f'Telefon — skaner mobilny #{session.pk}'})
        raw, _ = record_terminal_raw_scan(session=session, terminal=terminal,
            client_scan_id=client_scan_id, code=code, scanned_at=_parse_client_datetime(data.get('scanned_at')),
            current_location_code=current_location_code,
            allowed_location_ids=_get_location_subtree_ids(session.scope_root_locations.all()))
        result = dict(raw.processing_result)
        result['client_scan_id'] = client_scan_id
        result['raw_scan_id'] = raw.pk
    result["scan_count"] = InventoryObservedItem.objects.filter(session=session).count()
    return JsonResponse(result)


@login_required
@require_GET
def session_location_sheet_pdf(request, pk):
    from django.http import FileResponse, HttpResponse
    from locations.models import OrganizationSettings
    from locations.printing import generate_location_sheet_pdf

    session = get_object_or_404(get_visible_inventory_sessions(request.user), pk=pk)
    ids = _get_location_subtree_ids(session.scope_root_locations.all())
    allowed = get_accessible_location_ids(request.user)
    if allowed is not None:
        ids.intersection_update(allowed)
    locations = sorted(Location.objects.filter(pk__in=ids, is_active=True).select_related("parent"),
                       key=lambda location: (location.path.casefold(), location.pk))
    if not locations:
        return HttpResponse("Brak dostępnych aktywnych lokalizacji w zakresie tej inwentaryzacji.", status=400)
    organization = OrganizationSettings.objects.first()
    name = (organization.short_name or organization.full_name) if organization else ""
    return FileResponse(generate_location_sheet_pdf(locations, name or "LUPINE", session.number),
                        content_type="application/pdf", filename=f"lokalizacje-{session.number}.pdf")


TERMINAL_SHELL_CACHE = "lupine-terminal-shell-v5"


def _terminal_config():
    return {
        "sessions_url": reverse("inventory:terminal-sessions-api"),
        "bootstrap_url_template": reverse("inventory:terminal-session-bootstrap-api", kwargs={"session_id": 0}).replace("/0/", "/__SESSION_ID__/"),
        "terminal_create_url_template": reverse("inventory:terminal-create-api", kwargs={"session_id": 0}).replace("/0/", "/__SESSION_ID__/"),
        "sync_url_template": reverse("inventory:terminal-sync-api", kwargs={"session_id": 0, "terminal_id": 0}).replace("/0/terminals/0/", "/__SESSION_ID__/terminals/__TERMINAL_ID__/"),
        "context_url": reverse("inventory:terminal-context-api"),
        "logout_url": reverse("inventory:terminal-logout-api"),
        "app_url": reverse("inventory:terminal-app"),
        "offline_url": reverse("inventory:terminal-offline"),
        "worker_url": reverse("inventory:terminal-worker"),
        "client_url": reverse("inventory:terminal-client"),
        "shell_cache": TERMINAL_SHELL_CACHE,
    }


@require_GET
def terminal_offline_view(request):
    # Generic application shell only: no account, session data, or CSRF token.
    from django.http import HttpResponse
    from django.template.loader import render_to_string
    return HttpResponse(render_to_string("inventory/terminal_app.html", {"terminal_data": _terminal_config()}))


@require_GET
def terminal_worker_view(request):
    from django.http import HttpResponse
    from django.template.loader import render_to_string
    response = HttpResponse(render_to_string("inventory/terminal_worker.js", _terminal_config()), content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = reverse("inventory:terminal-app")
    return response


@require_GET
def terminal_client_view(request):
    from pathlib import Path
    from django.http import FileResponse
    response = FileResponse((Path(__file__).parent / "terminal_client.js").open("rb"), content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    return response


@login_required(login_url="inventory:terminal-login")
@require_GET
def terminal_context_api(request):
    from django.middleware.csrf import get_token
    response = JsonResponse({
        "ok": True,
        "user": {"id": request.user.pk, "username": request.user.get_username()},
        "csrf_token": get_token(request),
    })
    response["Cache-Control"] = "no-store"
    return response


@login_required(login_url="inventory:terminal-login")
@require_POST
def terminal_logout_api(request):
    from django.contrib.auth import logout
    logout(request)
    response = JsonResponse({"ok": True})
    response["Cache-Control"] = "no-store"
    return response

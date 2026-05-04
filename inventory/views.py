from collections import defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView, View

from accounts.utils import get_accessible_location_ids
from assets.models import Asset
from locations.models import Location

from .forms import DEFAULT_ASSET_TYPES, InventorySessionStartForm, SimpleInventorySessionStartForm
from .models import InventoryObservedItem, InventoryScanBatch, InventorySession
from .services import import_inventory_scan_text, start_inventory_session


class InventorySessionListView(LoginRequiredMixin, ListView):
    model = InventorySession
    template_name = "inventory/session_list.html"
    context_object_name = "sessions"

    def get_queryset(self):
        return get_visible_inventory_sessions(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asset_type_labels = dict(Asset.AssetType.choices)
        for session in context["sessions"]:
            session.root_locations_display = ", ".join(
                location.path for location in session.scope_root_locations.all()
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
        return get_visible_inventory_sessions(self.request.user).prefetch_related("snapshot_items")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        session = self.object
        asset_type_labels = dict(Asset.AssetType.choices)
        observed_items = list(
            InventoryObservedItem.objects
            .filter(session=session)
            .select_related("asset", "scanned_location")
            .order_by("-last_seen_at", "-id")
        )
        status_labels = {
            InventoryObservedItem.Status.FOUND_OK: "Zgodne",
            InventoryObservedItem.Status.FOUND_OTHER_LOCATION: "Inna lokalizacja",
            InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE: "Poza zakresem",
            InventoryObservedItem.Status.UNKNOWN_CODE: "Nieznany kod",
        }
        for item in observed_items:
            item.status_display_pl = status_labels.get(item.status, item.status)
            item.scanned_location_display = item.scanned_location.path if item.scanned_location else "-"

        observed_by_asset_id = {
            item.asset_id: item
            for item in observed_items
            if item.asset_id is not None
        }
        snapshot_items = list(session.snapshot_items.all())
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
            inventory_work_items.append(
                {
                    "snapshot": snapshot_item,
                    "observed": observed_item,
                    "display_status": display_status,
                    "scanned_location_display": scanned_location_display,
                    "last_seen_at": last_seen_at,
                }
            )

        context["page_title"] = session.number
        context["root_locations_display"] = ", ".join(
            location.path for location in session.scope_root_locations.all()
        )
        context["asset_type_scope_display"] = ", ".join(
            asset_type_labels.get(asset_type, asset_type)
            for asset_type in session.asset_type_scope
        )
        context["snapshot_items"] = snapshot_items
        context["snapshot_total"] = session.snapshot_items_count
        context["observed_assets_count"] = sum(1 for item in observed_items if item.asset_id is not None)
        context["found_ok_count"] = sum(1 for item in observed_items if item.status == InventoryObservedItem.Status.FOUND_OK)
        context["found_other_location_count"] = sum(
            1 for item in observed_items if item.status == InventoryObservedItem.Status.FOUND_OTHER_LOCATION
        )
        context["found_out_of_scope_count"] = sum(
            1 for item in observed_items if item.status == InventoryObservedItem.Status.FOUND_OUT_OF_SCOPE
        )
        context["unknown_code_count"] = sum(1 for item in observed_items if item.status == InventoryObservedItem.Status.UNKNOWN_CODE)
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
        return context


class InventorySessionCloseView(LoginRequiredMixin, View):
    def post(self, request, pk):
        session = get_object_or_404(get_visible_inventory_sessions(request.user), pk=pk)
        if session.status == InventorySession.Status.ACTIVE:
            session.status = InventorySession.Status.CLOSED
            session.closed_at = timezone.now()
            session.save(update_fields=["status", "closed_at", "updated_at"])
            messages.success(request, f"Zamknięto sesję inwentaryzacji {session.number}.")
        return redirect("inventory:session-detail", pk=session.pk)


@csrf_exempt
@require_POST
def scan_file_import_api(request):
    import_user = _get_scan_file_import_user(request)
    if import_user is None:
        if settings.DEBUG:
            return JsonResponse({"status": "error", "message": "No superuser is available for DEBUG import."}, status=403)
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)

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

    try:
        batch = import_inventory_scan_text(raw_text, uploaded_by=import_user)
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


def _get_scan_file_import_user(request):
    if request.user.is_authenticated:
        return request.user
    if settings.DEBUG:
        return get_user_model().objects.filter(is_superuser=True).order_by("id").first()
    return None


def _get_session_number_from_scan_text(raw_text: str) -> str:
    for line in raw_text.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def get_visible_inventory_sessions(user):
    queryset = (
        InventorySession.objects
        .select_related("created_by")
        .prefetch_related("scope_root_locations")
        .annotate(snapshot_items_count=Count("snapshot_items"))
        .order_by("-started_at", "-id")
    )

    accessible_location_ids = get_accessible_location_ids(user)
    if accessible_location_ids is None:
        return queryset
    if not accessible_location_ids:
        return queryset.none()

    visible_session_ids = []
    for session in queryset:
        session_location_ids = _get_location_subtree_ids(session.scope_root_locations.all())
        if session_location_ids.intersection(accessible_location_ids):
            visible_session_ids.append(session.id)

    return queryset.filter(id__in=visible_session_ids)


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

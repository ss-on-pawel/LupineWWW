from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, View

from accounts.utils import get_accessible_location_ids
from assets.models import Asset
from locations.models import Location

from .forms import DEFAULT_ASSET_TYPES, InventorySessionStartForm, SimpleInventorySessionStartForm
from .models import InventoryObservedItem, InventoryScanBatch, InventorySession
from .services import start_inventory_session


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

        context["page_title"] = session.number
        context["root_locations_display"] = ", ".join(
            location.path for location in session.scope_root_locations.all()
        )
        context["asset_type_scope_display"] = ", ".join(
            asset_type_labels.get(asset_type, asset_type)
            for asset_type in session.asset_type_scope
        )
        context["snapshot_items"] = session.snapshot_items.all()
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

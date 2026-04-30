import json

from accounts.utils import get_accessible_location_ids
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, TemplateView, UpdateView

from .filters import apply_asset_filters, get_asset_filter_ui_schema, parse_asset_filters
from .forms import AssetForm
from .models import Asset, AssetChangeRequest
from .services import (
    approve_asset_change_request,
    reject_asset_change_request,
    serialize_asset_form_payload,
    user_requires_asset_change_approval,
)
from locations.models import Location


def _user_can_review_asset_changes(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    try:
        profile = user.profile
    except ObjectDoesNotExist:
        return False
    return profile.can_approve_asset_changes


def _scope_asset_change_request_queryset(queryset, user):
    accessible_location_ids = get_accessible_location_ids(user)
    if accessible_location_ids is None:
        return queryset
    return queryset.filter(
        operation=AssetChangeRequest.Operation.UPDATE,
        asset__location_fk_id__in=accessible_location_ids,
    )


def get_asset_change_diff(change_request):
    payload = change_request.payload if isinstance(change_request.payload, dict) else {}
    if change_request.operation != AssetChangeRequest.Operation.UPDATE:
        return []

    current = payload.get("current")
    proposed = payload.get("proposed")
    if not isinstance(current, dict) or not isinstance(proposed, dict):
        return []

    field_names = sorted(set(current) | set(proposed))
    return [
        {
            "field": field_name,
            "current": current.get(field_name),
            "proposed": proposed.get(field_name),
        }
        for field_name in field_names
        if current.get(field_name) != proposed.get(field_name)
    ]


CHANGE_LIST_FIELD_LABELS = {
    "name": "Nazwa",
    "inventory_number": "Nr inw.",
    "value": "Wartość",
    "purchase_value": "Wartość",
    "location": "Lokalizacja",
    "status": "Status",
    "type": "Typ",
    "asset_type": "Typ",
    "condition": "Stan",
    "technical_condition": "Stan",
    "responsible_person": "Odpowiedzialny",
}

CHANGE_LIST_FIELD_ORDER = [
    "name",
    "inventory_number",
    "value",
    "purchase_value",
    "location",
    "status",
    "type",
    "asset_type",
    "condition",
    "technical_condition",
    "responsible_person",
]


def _format_change_list_value(value):
    if value is None:
        return "-"
    return value


def get_asset_change_list_summary(change_request):
    if change_request.operation == AssetChangeRequest.Operation.CREATE:
        return {"create": True, "rows": [], "remaining_count": 0}

    diff_rows = get_asset_change_diff(change_request)
    diff_by_field = {row["field"]: row for row in diff_rows}
    ordered_fields = [
        field_name
        for field_name in CHANGE_LIST_FIELD_ORDER
        if field_name in diff_by_field
    ]
    ordered_fields.extend(
        row["field"]
        for row in diff_rows
        if row["field"] not in ordered_fields
    )
    rows = [
        {
            "label": CHANGE_LIST_FIELD_LABELS.get(field_name, field_name),
            "current": _format_change_list_value(diff_by_field[field_name]["current"]),
            "proposed": _format_change_list_value(diff_by_field[field_name]["proposed"]),
        }
        for field_name in ordered_fields
    ]
    return {
        "create": False,
        "rows": rows[:3],
        "remaining_count": max(len(rows) - 3, 0),
    }


class AssetListView(LoginRequiredMixin, TemplateView):
    template_name = "assets/asset_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Ewidencja majątku"
        context["status_options"] = Asset.Status.choices
        context["location_options"] = list(
            Asset.objects.exclude(location="")
            .order_by("location")
            .values_list("location", flat=True)
            .distinct()
        )
        context["filter_schema"] = get_asset_filter_ui_schema()
        return context


class AssetChangeRequestListView(LoginRequiredMixin, ListView):
    model = AssetChangeRequest
    template_name = "assets/asset_change_list.html"
    context_object_name = "change_requests"
    paginate_by = 50

    def get_queryset(self):
        queryset = (
            AssetChangeRequest.objects
            .select_related("requested_by", "reviewed_by", "asset")
            .order_by("-created_at")
        )
        if _user_can_review_asset_changes(self.request.user):
            queryset = _scope_asset_change_request_queryset(queryset, self.request.user)
        else:
            queryset = queryset.filter(requested_by=self.request.user)

        status = self.request.GET.get("status", AssetChangeRequest.Status.PENDING)
        if status != "all":
            if status not in AssetChangeRequest.Status.values:
                status = AssetChangeRequest.Status.PENDING
            queryset = queryset.filter(status=status)

        operation = self.request.GET.get("operation", "all")
        if operation != "all":
            if operation in AssetChangeRequest.Operation.values:
                queryset = queryset.filter(operation=operation)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        status = self.request.GET.get("status", AssetChangeRequest.Status.PENDING)
        if status != "all" and status not in AssetChangeRequest.Status.values:
            status = AssetChangeRequest.Status.PENDING
        operation = self.request.GET.get("operation", "all")
        if operation != "all" and operation not in AssetChangeRequest.Operation.values:
            operation = "all"
        context["page_title"] = "Kolejka zmian"
        context["selected_status"] = status
        context["selected_operation"] = operation
        context["status_options"] = AssetChangeRequest.Status.choices
        context["operation_options"] = AssetChangeRequest.Operation.choices
        for change_request in context["change_requests"]:
            change_request.change_list_summary = get_asset_change_list_summary(change_request)
        return context


class AssetChangeRequestDetailView(LoginRequiredMixin, DetailView):
    model = AssetChangeRequest
    template_name = "assets/asset_change_detail.html"
    context_object_name = "change_request"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not _user_can_review_asset_changes(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        queryset = AssetChangeRequest.objects.select_related("requested_by", "reviewed_by", "asset")
        return _scope_asset_change_request_queryset(queryset, self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Szczegóły zmiany"
        context["change_diff"] = get_asset_change_diff(self.object)
        context["payload_items"] = (
            self.object.payload.items()
            if isinstance(self.object.payload, dict)
            else []
        )
        return context


@login_required
@require_POST
def asset_change_approve(request, pk):
    if not _user_can_review_asset_changes(request.user):
        raise PermissionDenied

    queryset = _scope_asset_change_request_queryset(
        AssetChangeRequest.objects.select_related("requested_by", "reviewed_by", "asset"),
        request.user,
    )
    change_request = get_object_or_404(queryset, pk=pk)

    try:
        approve_asset_change_request(change_request, request.user)
    except ValidationError:
        pass

    return redirect("assets:change-detail", pk=change_request.pk)


@login_required
@require_POST
def asset_change_reject(request, pk):
    if not _user_can_review_asset_changes(request.user):
        raise PermissionDenied

    queryset = _scope_asset_change_request_queryset(
        AssetChangeRequest.objects.select_related("requested_by", "reviewed_by", "asset"),
        request.user,
    )
    change_request = get_object_or_404(queryset, pk=pk)
    comment = request.POST.get("comment", "")

    try:
        reject_asset_change_request(change_request, request.user, comment)
    except ValidationError:
        pass

    return redirect("assets:change-detail", pk=change_request.pk)


class AssetCreateView(LoginRequiredMixin, CreateView):
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    success_url = reverse_lazy("assets:list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Dodaj składnik majątku"
        return context

    def form_valid(self, form):
        if user_requires_asset_change_approval(self.request.user):
            AssetChangeRequest.objects.create(
                requested_by=self.request.user,
                operation=AssetChangeRequest.Operation.CREATE,
                status=AssetChangeRequest.Status.PENDING,
                asset=None,
                payload=serialize_asset_form_payload(form.cleaned_data),
            )
            messages.success(self.request, "Zmiana została przekazana do akceptacji.")
            return redirect(self.success_url)

        messages.success(self.request, "Składnik majątku został zapisany.")
        return super().form_valid(form)


class AssetUpdateView(LoginRequiredMixin, UpdateView):
    model = Asset
    form_class = AssetForm
    template_name = "assets/asset_form.html"
    context_object_name = "asset"

    def get_queryset(self):
        queryset = super().get_queryset()
        accessible_location_ids = get_accessible_location_ids(self.request.user)
        if accessible_location_ids is None:
            return queryset
        return queryset.filter(location_fk_id__in=accessible_location_ids)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_title"] = "Edytuj składnik majątku"
        return context

    def form_valid(self, form):
        if user_requires_asset_change_approval(self.request.user):
            current_asset = self.get_queryset().get(pk=self.object.pk)
            current_payload = {
                field_name: getattr(current_asset, field_name)
                for field_name in form.fields
            }
            payload = {
                "current": serialize_asset_form_payload(current_payload),
                "proposed": serialize_asset_form_payload(form.cleaned_data),
            }
            pending_request = AssetChangeRequest.objects.filter(
                operation=AssetChangeRequest.Operation.UPDATE,
                status=AssetChangeRequest.Status.PENDING,
                asset=current_asset,
            ).first()
            if pending_request:
                pending_request.payload = payload
                pending_request.save(update_fields=["payload", "updated_at"])
                messages.success(self.request, "Oczekująca zmiana została zaktualizowana.")
            else:
                AssetChangeRequest.objects.create(
                    requested_by=self.request.user,
                    operation=AssetChangeRequest.Operation.UPDATE,
                    status=AssetChangeRequest.Status.PENDING,
                    asset=current_asset,
                    payload=payload,
                )
                messages.success(self.request, "Zmiana została przekazana do akceptacji.")
            return redirect(self.get_success_url())

        messages.success(self.request, "Składnik majątku został zaktualizowany.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("assets:detail", kwargs={"id": self.object.pk})


@login_required
def asset_detail(request, id):
    asset = get_object_or_404(Asset, pk=id)
    accessible_location_ids = get_accessible_location_ids(request.user)
    if accessible_location_ids is not None and asset.location_fk_id not in accessible_location_ids:
        raise Http404

    return render(
        request,
        "assets/asset_detail.html",
        {
            "asset": asset,
            "page_title": "Karta środka",
        },
    )


def asset_list_api(request):
    page = max(_parse_positive_int(request.GET.get("page"), default=1), 1)
    page_size = min(max(_parse_positive_int(request.GET.get("page_size"), default=50), 1), 200)
    search = request.GET.get("search", "").strip()
    status = request.GET.get("status", "").strip()
    location = request.GET.get("location", "").strip()
    ordering = request.GET.get("ordering", "-updated_at").strip() or "-updated_at"

    queryset = (
        Asset.objects.select_related("responsible_person", "current_user")
        .only(
            "id",
            "inventory_number",
            "name",
            "asset_type",
            "category",
            "manufacturer",
            "model",
            "serial_number",
            "barcode",
            "purchase_date",
            "commissioning_date",
            "purchase_value",
            "invoice_number",
            "external_id",
            "cost_center",
            "organizational_unit",
            "department",
            "location",
            "room",
            "status",
            "technical_condition",
            "last_inventory_date",
            "next_review_date",
            "warranty_until",
            "insurance_until",
            "is_active",
            "updated_at",
            "responsible_person__username",
            "responsible_person__first_name",
            "responsible_person__last_name",
            "current_user__username",
            "current_user__first_name",
            "current_user__last_name",
        )
    )

    location_ids = get_accessible_location_ids(request.user)
    if location_ids is not None:
        queryset = queryset.filter(location_fk_id__in=location_ids)

    if search:
        queryset = queryset.filter(
            Q(inventory_number__icontains=search)
            | Q(name__icontains=search)
            | Q(location__icontains=search)
        )

    if status:
        queryset = queryset.filter(status=status)

    if location:
        queryset = queryset.filter(location=location)

    parsed_filters = parse_asset_filters(request.GET)
    queryset = apply_asset_filters(queryset, parsed_filters)

    ordering_field = _resolve_asset_ordering(ordering)
    queryset = queryset.order_by(ordering_field, "id")

    paginator = Paginator(queryset, page_size)

    try:
        page_obj = paginator.page(page)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    results = [
        {
            "id": asset.id,
            "inventory_number": asset.inventory_number,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "asset_type_display": asset.get_asset_type_display(),
            "category": asset.category,
            "manufacturer": asset.manufacturer,
            "model": asset.model,
            "serial_number": asset.serial_number,
            "barcode": asset.barcode,
            "purchase_date": asset.purchase_date.isoformat() if asset.purchase_date else "",
            "commissioning_date": asset.commissioning_date.isoformat() if asset.commissioning_date else "",
            "purchase_value": str(asset.purchase_value) if asset.purchase_value is not None else "",
            "purchase_value_display": f"{asset.purchase_value} zł" if asset.purchase_value is not None else "-",
            "invoice_number": asset.invoice_number,
            "external_id": asset.external_id,
            "cost_center": asset.cost_center,
            "organizational_unit": asset.organizational_unit,
            "department": asset.department,
            "location": asset.location,
            "room": asset.room,
            "responsible_person": _format_person(asset.responsible_person),
            "current_user": _format_person(asset.current_user),
            "status": asset.status,
            "status_display": asset.get_status_display(),
            "technical_condition": asset.technical_condition,
            "technical_condition_display": asset.get_technical_condition_display(),
            "last_inventory_date": asset.last_inventory_date.isoformat() if asset.last_inventory_date else "",
            "next_review_date": asset.next_review_date.isoformat() if asset.next_review_date else "",
            "warranty_until": asset.warranty_until.isoformat() if asset.warranty_until else "",
            "insurance_until": asset.insurance_until.isoformat() if asset.insurance_until else "",
            "is_active": asset.is_active,
            "is_active_display": "Tak" if asset.is_active else "Nie",
            "updated_at": asset.updated_at.isoformat(),
            "updated_at_display": asset.updated_at.strftime("%Y-%m-%d %H:%M"),
        }
        for asset in page_obj.object_list
    ]

    return JsonResponse(
        {
            "results": results,
            "pagination": {
                "page": page_obj.number,
                "page_size": page_size,
                "total_pages": paginator.num_pages,
                "total_items": paginator.count,
                "has_next": page_obj.has_next(),
                "has_previous": page_obj.has_previous(),
            },
            "filters": {
                "search": search,
                "status": status,
                "location": location,
                "ordering": ordering_field,
            },
        }
    )


def asset_bulk_move_api(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Method not allowed."}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON body."}, status=400)

    raw_asset_ids = payload.get("asset_ids")
    if not isinstance(raw_asset_ids, list) or not raw_asset_ids:
        return JsonResponse({"success": False, "error": "asset_ids must be a non-empty list."}, status=400)

    asset_ids = []
    for raw_asset_id in raw_asset_ids:
        try:
            asset_id = int(raw_asset_id)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "asset_ids must contain valid asset IDs."}, status=400)
        if asset_id <= 0:
            return JsonResponse({"success": False, "error": "asset_ids must contain valid asset IDs."}, status=400)
        asset_ids.append(asset_id)

    if "target_location_id" not in payload:
        return JsonResponse({"success": False, "error": "target_location_id is required."}, status=400)

    raw_target_location_id = payload.get("target_location_id")
    try:
        target_location_id = int(raw_target_location_id)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "target_location_id must be a positive integer."}, status=400)

    if target_location_id <= 0:
        return JsonResponse({"success": False, "error": "target_location_id must be a positive integer."}, status=400)

    try:
        target_location = Location.objects.get(pk=target_location_id, is_active=True)
    except Location.DoesNotExist:
        return JsonResponse({"success": False, "error": "target_location_id does not point to an active location."}, status=400)

    unique_asset_ids = set(asset_ids)
    accessible_location_ids = get_accessible_location_ids(request.user)

    if accessible_location_ids is not None:
        if target_location.id not in accessible_location_ids:
            return JsonResponse({"success": False, "error": "Target location is outside your allowed scope."}, status=403)

        movable_assets = Asset.objects.filter(
            id__in=unique_asset_ids,
            location_fk_id__in=accessible_location_ids,
        )
        if movable_assets.count() != len(unique_asset_ids):
            return JsonResponse({"success": False, "error": "One or more assets are outside your allowed scope."}, status=403)
    else:
        movable_assets = Asset.objects.filter(id__in=unique_asset_ids)

    updated_count = movable_assets.update(
        location=target_location.path,
        location_fk=target_location,
    )

    return JsonResponse(
        {
            "success": True,
            "updated_count": updated_count,
            "target_location_id": target_location.id,
            "target_location_path": target_location.path,
        }
    )


def _format_person(user):
    if not user:
        return ""
    full_name = user.get_full_name().strip()
    return full_name or user.username


def _parse_positive_int(raw_value, default):
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _resolve_asset_ordering(raw_ordering):
    allowed_fields = {
        "inventory_number": "inventory_number",
        "name": "name",
        "asset_type": "asset_type",
        "status": "status",
        "location": "location",
        "category": "category",
        "manufacturer": "manufacturer",
        "model": "model",
        "serial_number": "serial_number",
        "barcode": "barcode",
        "department": "department",
        "organizational_unit": "organizational_unit",
        "room": "room",
        "responsible_person": "responsible_person__username",
        "current_user": "current_user__username",
        "technical_condition": "technical_condition",
        "purchase_date": "purchase_date",
        "commissioning_date": "commissioning_date",
        "purchase_value": "purchase_value",
        "value": "purchase_value",
        "invoice_number": "invoice_number",
        "external_id": "external_id",
        "cost_center": "cost_center",
        "last_inventory_date": "last_inventory_date",
        "next_review_date": "next_review_date",
        "warranty_until": "warranty_until",
        "insurance_until": "insurance_until",
        "is_active": "is_active",
        "updated_at": "updated_at",
    }
    is_desc = raw_ordering.startswith("-")
    field = raw_ordering[1:] if is_desc else raw_ordering
    resolved = allowed_fields.get(field, "updated_at")
    return f"-{resolved}" if is_desc else resolved

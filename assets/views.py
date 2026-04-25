import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, TemplateView

from .filters import apply_asset_filters, get_asset_filter_ui_schema, parse_asset_filters
from .forms import AssetForm
from .models import Asset
from locations.models import Location


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
        messages.success(self.request, "Składnik majątku został zapisany.")
        return super().form_valid(form)


@login_required
def asset_detail(request, id):
    asset = get_object_or_404(Asset, pk=id)
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

    updated_count = Asset.objects.filter(id__in=set(asset_ids)).update(
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

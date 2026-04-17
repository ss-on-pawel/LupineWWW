from django.contrib import messages
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, TemplateView

from .forms import AssetForm
from .models import Asset


class AssetListView(TemplateView):
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
        return context


class AssetCreateView(CreateView):
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

    queryset = Asset.objects.only(
        "id",
        "inventory_number",
        "name",
        "status",
        "location",
        "category",
        "updated_at",
        "purchase_value",
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
            "status": asset.status,
            "status_display": asset.get_status_display(),
            "location": asset.location,
            "category": asset.category,
            "updated_at": asset.updated_at.isoformat(),
            "updated_at_display": asset.updated_at.strftime("%Y-%m-%d %H:%M"),
            "value": str(asset.purchase_value) if asset.purchase_value is not None else "",
            "value_display": f"{asset.purchase_value} zł" if asset.purchase_value is not None else "-",
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
        "status": "status",
        "location": "location",
        "category": "category",
        "updated_at": "updated_at",
        "value": "purchase_value",
        "purchase_value": "purchase_value",
    }
    is_desc = raw_ordering.startswith("-")
    field = raw_ordering[1:] if is_desc else raw_ordering
    resolved = allowed_fields.get(field, "updated_at")
    return f"-{resolved}" if is_desc else resolved

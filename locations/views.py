from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.http import Http404, FileResponse, HttpResponse
from django.views.decorators.http import require_GET
from django.shortcuts import redirect, render
from django.urls import reverse

from accounts.utils import get_accessible_location_ids
from assets.models import Asset

from .forms import LocationForm, OrganizationSettingsForm
from .models import Location, OrganizationSettings


@login_required
def location_list(request):
    query = request.GET.get("q", "").strip()
    queryset = Location.objects.select_related("parent").order_by("name", "id")

    if query:
        queryset = queryset.filter(
            Q(name__icontains=query)
            | Q(code__icontains=query)
            | Q(parent__name__icontains=query)
        )

    locations = list(queryset)
    allowed = get_accessible_location_ids(request.user)
    for location in locations:
        location.can_print = location.is_active and (allowed is None or location.pk in allowed)

    if query:
        lowered_query = query.lower()
        locations = [
            location
            for location in locations
            if lowered_query in location.path.lower()
            or lowered_query in location.name.lower()
            or lowered_query in location.code.lower()
            or lowered_query in location.parent_name.lower()
        ]
        for location in locations:
            location.depth = 0
        rendered_locations = locations
    else:
        rendered_locations = _build_location_tree_rows(locations)

    return render(
        request,
        "locations/location_list.html",
        {
            "page_title": "Lokalizacje",
            "locations": rendered_locations,
            "search_query": query,
        },
    )


@login_required
def location_detail(request, id):
    location = _get_location(id)
    assigned_assets = list(
        _get_assigned_assets_queryset(location).only("id", "inventory_number", "name", "status", "location")
    )

    return render(
        request,
        "locations/location_detail.html",
        {
            "page_title": location.name,
            "location": location,
            "breadcrumbs": location.get_ancestors(include_self=True),
            "assigned_assets": assigned_assets,
            "assigned_assets_count": len(assigned_assets),
            "can_print": location.is_active and (
                (allowed := get_accessible_location_ids(request.user)) is None or location.pk in allowed
            ),
        },
    )


@login_required
def location_create(request):
    form = LocationForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        location = form.save()
        messages.success(request, "Lokalizacja została dodana.")
        return redirect("locations:detail", id=location.id)

    return render(
        request,
        "locations/location_form.html",
        {
            "page_title": "Nowa lokalizacja",
            "form_title": "Nowa lokalizacja",
            "form_intro": "Dodaj nową lokalizację główną jako początek kolejnej gałęzi struktury.",
            "parent_location": None,
            "form": form,
            "cancel_url": reverse("locations:list"),
        },
    )


@login_required
def location_create_child(request, id):
    parent_location = _get_location(id)
    form = LocationForm(request.POST or None, parent=parent_location)

    if request.method == "POST" and form.is_valid():
        location = form.save()
        messages.success(request, "Podlokalizacja została dodana.")
        return redirect("locations:detail", id=location.id)

    return render(
        request,
        "locations/location_form.html",
        {
            "page_title": "Nowa podlokalizacja",
            "form_title": "Nowa podlokalizacja",
            "form_intro": "Dodaj nową podlokalizację w aktualnym miejscu struktury.",
            "parent_location": parent_location,
            "form": form,
            "cancel_url": reverse("locations:detail", kwargs={"id": parent_location.id}),
        },
    )


@login_required
def location_update(request, id):
    location = _get_location(id)
    form = LocationForm(request.POST or None, instance=location, parent=location.parent)

    if request.method == "POST" and form.is_valid():
        location = form.save()
        messages.success(request, "Lokalizacja została zaktualizowana.")
        return redirect("locations:detail", id=location.id)

    return render(
        request,
        "locations/location_form.html",
        {
            "page_title": f"Edycja: {location.name}",
            "form_title": "Edytuj lokalizację",
            "form_intro": "Zmień nazwę lokalizacji bez naruszania jej miejsca w strukturze.",
            "parent_location": location.parent,
            "form": form,
            "cancel_url": reverse("locations:detail", kwargs={"id": location.id}),
        },
    )


@login_required
def location_delete(request, id):
    location = _get_location(id)

    if request.method != "POST":
        return redirect("locations:detail", id=location.id)

    blockers = _get_location_delete_blockers(location)
    if blockers:
        messages.error(request, "Nie można usunąć lokalizacji: " + "; ".join(blockers))
        return redirect("locations:detail", id=location.id)

    location.delete()
    messages.success(request, "Lokalizacja została usunięta.")
    return redirect("locations:list")


def location_options_api(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Method not allowed."}, status=405)

    queryset = Location.objects.select_related("parent").filter(is_active=True)
    location_ids = get_accessible_location_ids(request.user)
    if location_ids is not None:
        queryset = queryset.filter(id__in=location_ids)

    locations = list(queryset)
    rows = [
        {
            "id": location.id,
            "path": location.path,
        }
        for location in locations
    ]
    rows.sort(key=lambda item: (item["path"].lower(), item["id"]))

    return JsonResponse({"locations": rows})


@login_required
def organization_settings(request):
    org = OrganizationSettings.get()
    form = OrganizationSettingsForm(request.POST or None, instance=org)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Dane organizacji zostały zapisane.")
        return redirect("locations:organization-settings")
    return render(
        request,
        "locations/organization_settings.html",
        {
            "page_title": "Dane organizacji",
            "form": form,
        },
    )


def _get_location(id):
    try:
        return Location.objects.select_related("parent").get(pk=id)
    except Location.DoesNotExist as exc:
        raise Http404("Nie znaleziono lokalizacji.") from exc


def _build_location_tree_rows(locations):
    children_by_parent_id = {}
    for location in locations:
        children_by_parent_id.setdefault(location.parent_id, []).append(location)

    ordered_locations = []

    def walk(parent_id, depth):
        for location in children_by_parent_id.get(parent_id, []):
            location.depth = depth
            ordered_locations.append(location)
            walk(location.id, depth + 1)

    walk(None, 0)
    return ordered_locations


def _get_location_delete_blockers(location):
    blockers = []

    if location.children.exists():
        blockers.append("lokalizacja zawiera podlokalizacje")

    if _count_assigned_assets(location):
        blockers.append("do lokalizacji są przypisane środki")

    return blockers


def _count_assigned_assets(location):
    return _get_assigned_assets_queryset(location).count()


def _get_assigned_assets_queryset(location):
    # Safe integration point until Asset gets a real FK to Location.
    return Asset.objects.filter(
        Q(location__iexact=location.name) | Q(location__iexact=location.path)
    ).order_by("inventory_number", "id")


@login_required
@require_GET
def location_labels_pdf(request):
    from .printing import generate_location_labels_pdf

    raw_ids = request.GET.getlist("ids")
    try:
        ids = {int(value) for raw in raw_ids for value in raw.split(",")}
    except (TypeError, ValueError):
        return HttpResponse("Nieprawidłowe identyfikatory lokalizacji.", status=400)
    if not ids or len(ids) > 5000 or min(ids) < 1:
        return HttpResponse("Zaznacz od 1 do 5000 lokalizacji.", status=400)
    queryset = Location.objects.filter(pk__in=ids, is_active=True).select_related("parent")
    allowed = get_accessible_location_ids(request.user)
    if allowed is not None:
        queryset = queryset.filter(pk__in=allowed)
    locations = sorted(queryset, key=lambda location: (location.path.casefold(), location.pk))
    if len(locations) != len(ids):
        raise Http404("Nie znaleziono lokalizacji lub brak dostępu.")
    organization = OrganizationSettings.objects.first()
    name = (organization.short_name or organization.full_name) if organization else ""
    return FileResponse(generate_location_labels_pdf(locations, name or "LUPINE"),
                        content_type="application/pdf", filename="etykiety-lokalizacji.pdf")

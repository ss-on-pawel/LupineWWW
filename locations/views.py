from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from assets.models import Asset

from .forms import LocationForm
from .models import Location


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
        },
    )


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

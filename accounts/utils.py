from collections import defaultdict

from locations.models import Location


def get_accessible_location_ids(user):
    if not getattr(user, "is_authenticated", False):
        return set()

    if user.is_superuser:
        return None

    profile = getattr(user, "profile", None)
    if profile is None:
        return set()

    if profile.role == profile.Role.ADMIN:
        return None

    root_ids = list(profile.allowed_locations.values_list("id", flat=True))
    if not root_ids:
        return set()

    children_by_parent_id = defaultdict(list)
    for location_id, parent_id in Location.objects.values_list("id", "parent_id"):
        children_by_parent_id[parent_id].append(location_id)

    accessible_ids = set()
    pending_ids = list(root_ids)

    while pending_ids:
        current_id = pending_ids.pop()
        if current_id in accessible_ids:
            continue
        accessible_ids.add(current_id)
        pending_ids.extend(children_by_parent_id.get(current_id, ()))

    return accessible_ids

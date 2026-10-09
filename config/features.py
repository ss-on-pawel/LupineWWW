from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.core.exceptions import PermissionDenied


FULL = "full"
LITE = "lite"
UI_MODE_SESSION_KEY = "lupine_ui_mode"


FEATURES = {
    "assets_core",
    "asset_archive",
    "asset_attachments",
    "asset_import",
    "asset_labels",
    "asset_lt_documents",
    "asset_type_settings",
    "asset_withdraw",
    "approval_queue",
    "depreciation",
    "inventory_core",
    "inventory_reports",
    "locations_core",
    "service_alerts",
    "terminal",
    "user_admin",
}

LITE_DISABLED_FEATURES = {
    "asset_archive",
    "asset_attachments",
    "asset_import",
    "asset_labels",
    "asset_lt_documents",
    "asset_type_settings",
    "approval_queue",
    "depreciation",
    "inventory_reports",
    "service_alerts",
}


def get_product_edition() -> str:
    edition = str(getattr(settings, "LUPINE_EDITION", FULL) or FULL).strip().lower()
    return edition if edition in {FULL, LITE} else FULL


def get_ui_mode(request) -> str:
    if get_product_edition() == LITE:
        return LITE
    if request is not None and getattr(request, "session", None):
        if request.session.get(UI_MODE_SESSION_KEY) == LITE:
            return LITE
        if request.session.get(UI_MODE_SESSION_KEY) == FULL:
            return FULL
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False):
        profile = getattr(user, "profile", None)
        if profile is not None and getattr(profile, "interface_mode", FULL) == LITE:
            return LITE
    return FULL


def user_can_switch_ui_mode(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and getattr(profile, "role", None) == profile.Role.ADMIN)


def is_lite_mode(request) -> bool:
    return get_ui_mode(request) == LITE


def feature_enabled(request, feature_key: str) -> bool:
    if feature_key not in FEATURES:
        return False
    if not is_lite_mode(request):
        return True
    return feature_key not in LITE_DISABLED_FEATURES


def require_feature(request, feature_key: str) -> None:
    if not feature_enabled(request, feature_key):
        raise PermissionDenied


def feature_required(feature_key: str):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            require_feature(request, feature_key)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


class FeatureRequiredMixin:
    feature_key = None

    def dispatch(self, request, *args, **kwargs):
        if self.feature_key:
            require_feature(request, self.feature_key)
        return super().dispatch(request, *args, **kwargs)


def build_feature_map(request) -> dict[str, bool]:
    return {feature_key: feature_enabled(request, feature_key) for feature_key in FEATURES}


def feature_context(request) -> dict:
    return {
        "features": build_feature_map(request),
        "lupine_product_edition": get_product_edition(),
        "lupine_ui_mode": get_ui_mode(request),
        "lupine_is_lite_mode": is_lite_mode(request),
        "lupine_can_switch_ui_mode": user_can_switch_ui_mode(getattr(request, "user", None)),
    }

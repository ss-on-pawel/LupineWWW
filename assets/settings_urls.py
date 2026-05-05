from django.urls import path

from .views import (
    AssetTypeCreateView,
    AssetTypeListView,
    AssetTypeUpdateView,
    asset_type_activate,
    asset_type_deactivate,
)


app_name = "settings"

urlpatterns = [
    path("asset-types/", AssetTypeListView.as_view(), name="asset-types"),
    path("asset-types/add/", AssetTypeCreateView.as_view(), name="asset-type-create"),
    path("asset-types/<int:pk>/edit/", AssetTypeUpdateView.as_view(), name="asset-type-update"),
    path("asset-types/<int:pk>/deactivate/", asset_type_deactivate, name="asset-type-deactivate"),
    path("asset-types/<int:pk>/activate/", asset_type_activate, name="asset-type-activate"),
]

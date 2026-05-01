from django.urls import path

from .views import (
    AssetChangeRequestListView,
    AssetChangeRequestDetailView,
    AssetCreateView,
    AssetListView,
    AssetUpdateView,
    asset_change_approve,
    asset_change_bulk_approve,
    asset_change_reject,
    asset_bulk_move_api,
    asset_detail,
    asset_list_api,
)


app_name = "assets"

urlpatterns = [
    path("api/assets/", asset_list_api, name="api-list"),
    path("api/assets/bulk-move/", asset_bulk_move_api, name="api-bulk-move"),
    path("", AssetListView.as_view(), name="list"),
    path("assets/changes/", AssetChangeRequestListView.as_view(), name="change-list"),
    path("assets/changes/bulk-approve/", asset_change_bulk_approve, name="bulk-approve"),
    path("assets/changes/<int:pk>/", AssetChangeRequestDetailView.as_view(), name="change-detail"),
    path("assets/changes/<int:pk>/approve/", asset_change_approve, name="change-approve"),
    path("assets/changes/<int:pk>/reject/", asset_change_reject, name="change-reject"),
    path("assets/add/", AssetCreateView.as_view(), name="create"),
    path("assets/<int:pk>/edit/", AssetUpdateView.as_view(), name="update"),
    path("assets/<int:id>/", asset_detail, name="detail"),
]

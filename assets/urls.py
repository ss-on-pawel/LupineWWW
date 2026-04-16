from django.urls import path

from .views import AssetCreateView, AssetListView, asset_detail, asset_list_api


app_name = "assets"

urlpatterns = [
    path("api/assets/", asset_list_api, name="api-list"),
    path("", AssetListView.as_view(), name="list"),
    path("assets/add/", AssetCreateView.as_view(), name="create"),
    path("assets/<int:id>/", asset_detail, name="detail"),
]

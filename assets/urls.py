from django.urls import path

from .views import AssetCreateView, AssetListView, asset_detail


app_name = "assets"

urlpatterns = [
    path("", AssetListView.as_view(), name="list"),
    path("assets/add/", AssetCreateView.as_view(), name="create"),
    path("assets/<int:id>/", asset_detail, name="detail"),
]

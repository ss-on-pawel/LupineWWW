from django.urls import path

from .views import AssetCreateView, AssetListView


app_name = "assets"

urlpatterns = [
    path("", AssetListView.as_view(), name="list"),
    path("assets/add/", AssetCreateView.as_view(), name="create"),
]

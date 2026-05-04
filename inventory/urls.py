from django.urls import path

from .views import (
    InventorySessionCloseView,
    InventorySessionDetailView,
    InventorySessionListView,
    InventorySessionStartView,
)


app_name = "inventory"

urlpatterns = [
    path("", InventorySessionListView.as_view(), name="session-list"),
    path("start/", InventorySessionStartView.as_view(), name="session-start"),
    path("<int:pk>/", InventorySessionDetailView.as_view(), name="session-detail"),
    path("<int:pk>/close/", InventorySessionCloseView.as_view(), name="session-close"),
]

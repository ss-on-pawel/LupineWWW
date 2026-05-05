from django.urls import path

from .views import (
    InventorySessionCloseView,
    InventorySessionDetailView,
    InventorySessionListView,
    InventorySessionStartView,
    manual_quantity_api,
    scan_file_import_api,
)


app_name = "inventory"

urlpatterns = [
    path("api/inventory/scan-files/", scan_file_import_api, name="scan-file-import-api"),
    path("api/inventory/sessions/<int:session_id>/manual-quantity/", manual_quantity_api, name="manual-quantity-api"),
    path("inventory/", InventorySessionListView.as_view(), name="session-list"),
    path("inventory/start/", InventorySessionStartView.as_view(), name="session-start"),
    path("inventory/<int:pk>/", InventorySessionDetailView.as_view(), name="session-detail"),
    path("inventory/<int:pk>/close/", InventorySessionCloseView.as_view(), name="session-close"),
]

from django.urls import path

from .views import (
    InventorySessionCloseView,
    InventorySessionDetailView,
    InventorySessionDiscrepancyReportView,
    InventorySessionListView,
    InventorySessionReportView,
    InventorySessionSheetView,
    InventorySessionStartView,
    apply_inventory_session_to_assets,
    manual_confirmation_api,
    manual_quantity_api,
    mobile_scan_api,
    mobile_scan_view,
    scan_file_import_api,
)


app_name = "inventory"

urlpatterns = [
    path("api/inventory/scan-files/", scan_file_import_api, name="scan-file-import-api"),
    path("api/inventory/sessions/<int:session_id>/manual-quantity/", manual_quantity_api, name="manual-quantity-api"),
    path("api/inventory/sessions/<int:session_id>/manual-confirmation/", manual_confirmation_api, name="manual-confirmation-api"),
    path("inventory/", InventorySessionListView.as_view(), name="session-list"),
    path("inventory/start/", InventorySessionStartView.as_view(), name="session-start"),
    path("inventory/<int:pk>/", InventorySessionDetailView.as_view(), name="session-detail"),
    path("inventory/<int:pk>/report/", InventorySessionReportView.as_view(), name="session-report"),
    path("inventory/<int:pk>/discrepancies/", InventorySessionDiscrepancyReportView.as_view(), name="session-discrepancy-report"),
    path("inventory/<int:pk>/sheet/", InventorySessionSheetView.as_view(), name="session-sheet"),
    path("inventory/<int:pk>/close/", InventorySessionCloseView.as_view(), name="session-close"),
    path("inventory/<int:pk>/apply-to-assets/", apply_inventory_session_to_assets, name="session-apply-to-assets"),
    path("inventory/mobile-scan/<str:token>/", mobile_scan_view, name="mobile-scan"),
    path("inventory/mobile-scan/<str:token>/api/scan/", mobile_scan_api, name="mobile-scan-api"),
]

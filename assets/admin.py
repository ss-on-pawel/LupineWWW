from django.contrib import admin

from .models import Asset, AssetChangeRequest


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_per_page = 50
    date_hierarchy = "created_at"
    empty_value_display = "-"
    list_display = (
        "inventory_number",
        "name",
        "asset_type",
        "category",
        "status",
        "technical_condition",
        "location",
        "responsible_person",
        "is_active",
        "created_at",
    )
    list_filter = (
        "asset_type",
        "category",
        "status",
        "technical_condition",
        "is_active",
        "organizational_unit",
        "department",
        "location",
        "created_at",
    )
    search_fields = (
        "=inventory_number",
        "name",
        "serial_number",
        "barcode",
        "external_id",
        "invoice_number",
        "manufacturer",
        "model",
        "cost_center",
        "organizational_unit",
        "department",
        "location",
        "room",
        "description",
        "responsible_person__username",
        "responsible_person__first_name",
        "responsible_person__last_name",
        "current_user__username",
        "current_user__first_name",
        "current_user__last_name",
    )
    autocomplete_fields = ("responsible_person", "current_user")
    list_select_related = ("responsible_person", "current_user")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-created_at",)
    fieldsets = (
        (
            "Dane podstawowe",
            {
                "fields": (
                    "name",
                    "inventory_number",
                    "asset_type",
                    "category",
                    "description",
                    "is_active",
                )
            },
        ),
        (
            "Identyfikacja i specyfikacja",
            {
                "fields": (
                    "manufacturer",
                    "model",
                    "serial_number",
                    "barcode",
                    "external_id",
                )
            },
        ),
        (
            "Dane zakupowe",
            {
                "fields": (
                    "purchase_date",
                    "commissioning_date",
                    "purchase_value",
                    "invoice_number",
                )
            },
        ),
        (
            "Przypisanie organizacyjne",
            {
                "fields": (
                    "cost_center",
                    "organizational_unit",
                    "department",
                    "location",
                    "room",
                    "responsible_person",
                    "current_user",
                )
            },
        ),
        (
            "Eksploatacja",
            {
                "fields": (
                    "status",
                    "technical_condition",
                    "last_inventory_date",
                    "next_review_date",
                    "warranty_until",
                    "insurance_until",
                )
            },
        ),
        (
            "Metadane",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )


@admin.register(AssetChangeRequest)
class AssetChangeRequestAdmin(admin.ModelAdmin):
    list_display = (
        "operation",
        "status",
        "requested_by",
        "asset",
        "created_at",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = ("operation", "status", "created_at", "reviewed_at")
    search_fields = (
        "requested_by__username",
        "requested_by__first_name",
        "requested_by__last_name",
        "asset__name",
        "asset__inventory_number",
        "review_comment",
    )
    autocomplete_fields = ("requested_by", "asset", "reviewed_by")
    list_select_related = ("requested_by", "asset", "reviewed_by")
    readonly_fields = ("created_at", "updated_at", "reviewed_at")
    ordering = ("-created_at",)

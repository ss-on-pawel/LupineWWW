from django.contrib import admin

from .models import Asset


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("name", "inventory_number", "added_at")
    search_fields = ("name", "inventory_number", "description")
    list_filter = ("added_at",)
    ordering = ("-added_at",)

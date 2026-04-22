from django.contrib import admin

from .models import Location


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "parent", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "code", "parent__name")
    readonly_fields = ("code", "created_at", "updated_at")
    autocomplete_fields = ("parent",)
    ordering = ("name", "id")


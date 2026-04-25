from django.contrib import admin

from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "can_approve_asset_changes")
    list_filter = ("role", "can_approve_asset_changes")
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email")
    filter_horizontal = ("allowed_locations",)

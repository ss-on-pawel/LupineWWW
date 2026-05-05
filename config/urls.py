from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("settings/", include("assets.settings_urls")),
    path("", include("inventory.urls")),
    path("", include("assets.urls")),
    path("", include("locations.urls")),
]

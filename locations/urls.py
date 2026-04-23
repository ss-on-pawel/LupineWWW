from django.urls import path

from .views import (
    location_create,
    location_create_child,
    location_delete,
    location_detail,
    location_list,
    location_options_api,
    location_update,
)


app_name = "locations"

urlpatterns = [
    path("api/locations/options/", location_options_api, name="api-options"),
    path("lokalizacje/", location_list, name="list"),
    path("lokalizacje/nowa/", location_create, name="create"),
    path("lokalizacje/<int:id>/nowa-podrzedna/", location_create_child, name="create-child"),
    path("lokalizacje/<int:id>/edytuj/", location_update, name="update"),
    path("lokalizacje/<int:id>/usun/", location_delete, name="delete"),
    path("lokalizacje/<int:id>/", location_detail, name="detail"),
]

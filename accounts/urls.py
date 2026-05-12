from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("users/", views.user_list, name="user-list"),
    path("users/new/", views.user_create, name="user-create"),
    path("users/<int:pk>/edit/", views.user_edit, name="user-edit"),
    path("users/<int:pk>/reset-password/", views.user_reset_password, name="user-reset-password"),
]

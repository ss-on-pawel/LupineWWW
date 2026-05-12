import secrets
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import UserCreateForm, UserEditForm
from .mail import send_system_email

User = get_user_model()

# Alphabet without visually confusing chars: O≈0, I≈1, l≈1
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_temp_password():
    part1 = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    part2 = "".join(secrets.choice(_ALPHABET) for _ in range(4))
    return f"LUP-{part1}-{part2}"


def _is_panel_admin(user):
    if user.is_superuser or user.is_staff:
        return True
    try:
        return user.profile.role == "admin"
    except Exception:
        return False


def panel_admin_required(view_func):
    @wraps(view_func)
    def check_admin(request, *args, **kwargs):
        if _is_panel_admin(request.user):
            return view_func(request, *args, **kwargs)
        return HttpResponseForbidden()

    return login_required(check_admin)


@panel_admin_required
def user_list(request):
    users = (
        User.objects.select_related("profile")
        .prefetch_related("profile__allowed_locations")
        .order_by("username")
    )
    return render(request, "accounts/user_list.html", {"users": users})


@panel_admin_required
def user_create(request):
    if request.method == "POST":
        form = UserCreateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Użytkownik został utworzony.")
            return redirect("accounts:user-list")
    else:
        form = UserCreateForm()
    return render(request, "accounts/user_form.html", {"form": form, "is_create": True})


@panel_admin_required
def user_edit(request, pk):
    edited_user = get_object_or_404(User, pk=pk)
    if request.method == "POST":
        form = UserEditForm(request.POST, instance=edited_user)
        if form.is_valid():
            form.save()
            messages.success(request, "Dane użytkownika zostały zaktualizowane.")
            return redirect("accounts:user-list")
    else:
        form = UserEditForm(instance=edited_user)
    return render(
        request,
        "accounts/user_form.html",
        {"form": form, "is_create": False, "edited_user": edited_user},
    )


@panel_admin_required
@require_POST
def user_reset_password(request, pk):
    user = get_object_or_404(User, pk=pk)
    temp_password = generate_temp_password()
    user.set_password(temp_password)
    user.save(update_fields=["password"])

    if user.email:
        body = _build_reset_email_body(user.username, temp_password)
        try:
            send_system_email(
                subject="LupineWWW — nowe hasło",
                body=body,
                recipients=[user.email],
            )
            messages.success(
                request,
                f"Nowe hasło zostało wygenerowane i wysłane mailem na adres {user.email}.",
            )
        except Exception:
            messages.warning(
                request,
                f"Reset hasła OK, ale wysyłka maila nieudana. "
                f"Hasło tymczasowe (zachowaj i przekaż bezpiecznie): {temp_password}",
            )
    else:
        messages.warning(
            request,
            f"Użytkownik {user.username} nie ma adresu email. "
            f"Hasło tymczasowe (zachowaj i przekaż bezpiecznie): {temp_password}",
        )

    return redirect("accounts:user-list")


def _build_reset_email_body(username, temp_password):
    lines = [
        "Zostało wygenerowane nowe hasło tymczasowe dla konta w systemie LupineWWW.",
        "",
        f"Login: {username}",
        f"Hasło: {temp_password}",
        "",
        "Zmień hasło po pierwszym logowaniu.",
    ]
    site_url = getattr(settings, "SITE_URL", None)
    if site_url:
        lines += ["", f"System: {site_url}"]
    return "\n".join(lines)

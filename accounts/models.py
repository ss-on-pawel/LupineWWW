from django.conf import settings
from django.db import models

from locations.models import Location


class UserProfile(models.Model):
    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        MANAGER = "manager", "Zarzadzajacy"
        USER = "user", "Uzytkownik"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        verbose_name="Uzytkownik",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.USER,
        verbose_name="Rola",
    )
    allowed_locations = models.ManyToManyField(
        Location,
        blank=True,
        related_name="authorized_user_profiles",
        verbose_name="Dopuszczone korzenie lokalizacji",
        help_text="Przypisz korzenie dostepu. Dziedziczenie do dzieci i wnukow bedzie obslugiwane w kolejnym etapie.",
    )
    can_approve_asset_changes = models.BooleanField(
        default=False,
        verbose_name="Moze zatwierdzac zmiany srodkow",
    )
    asset_changes_require_approval = models.BooleanField(
        default=False,
        verbose_name="Zmiany srodkow wymagaja akceptacji",
    )

    class Meta:
        verbose_name = "Profil uzytkownika"
        verbose_name_plural = "Profile uzytkownikow"

    def __str__(self) -> str:
        return f"Profil: {self.user.get_username()}"

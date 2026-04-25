from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def create_missing_profiles(apps, schema_editor):
    user_model = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    user_profile_model = apps.get_model("accounts", "UserProfile")

    existing_user_ids = set(user_profile_model.objects.values_list("user_id", flat=True))
    missing_profiles = [
        user_profile_model(user_id=user.id)
        for user in user_model.objects.all().only("id")
        if user.id not in existing_user_ids
    ]
    if missing_profiles:
        user_profile_model.objects.bulk_create(missing_profiles)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("locations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("admin", "Administrator"), ("manager", "Zarzadzajacy"), ("user", "Uzytkownik")], default="user", max_length=20, verbose_name="Rola")),
                ("can_approve_asset_changes", models.BooleanField(default=False, verbose_name="Moze zatwierdzac zmiany srodkow")),
                ("allowed_locations", models.ManyToManyField(blank=True, help_text="Przypisz korzenie dostepu. Dziedziczenie do dzieci i wnukow bedzie obslugiwane w kolejnym etapie.", related_name="authorized_user_profiles", to="locations.location", verbose_name="Dopuszczone korzenie lokalizacji")),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="profile", to=settings.AUTH_USER_MODEL, verbose_name="Uzytkownik")),
            ],
            options={
                "verbose_name": "Profil uzytkownika",
                "verbose_name_plural": "Profile uzytkownikow",
            },
        ),
        migrations.RunPython(create_missing_profiles, migrations.RunPython.noop),
    ]

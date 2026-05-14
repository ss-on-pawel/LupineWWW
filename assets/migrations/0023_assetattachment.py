import assets.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0022_alter_asset_inventory_number_non_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "file",
                    models.FileField(
                        upload_to=assets.models._attachment_upload_path,
                        verbose_name="Plik",
                    ),
                ),
                ("title", models.CharField(max_length=255, verbose_name="Tytuł")),
                ("original_filename", models.CharField(max_length=255, verbose_name="Oryginalna nazwa pliku")),
                ("content_type", models.CharField(max_length=120, verbose_name="Typ MIME")),
                ("size_bytes", models.PositiveIntegerField(verbose_name="Rozmiar (bajty)")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, verbose_name="Data dodania")),
                (
                    "asset",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="assets.asset",
                        verbose_name="Środek",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="uploaded_attachments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Dodany przez",
                    ),
                ),
            ],
            options={
                "verbose_name": "Załącznik",
                "verbose_name_plural": "Załączniki",
                "ordering": ["-uploaded_at"],
            },
        ),
    ]

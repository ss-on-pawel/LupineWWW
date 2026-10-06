from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("locations", "0002_add_organization_settings"),
        ("inventory", "0007_inventorysession_mobile_scan_token"),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryScanTerminal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80)),
                ("description", models.CharField(blank=True, max_length=255)),
                ("is_active", models.BooleanField(default=True)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="inventory_scan_terminals",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="scan_terminals",
                        to="inventory.inventorysession",
                    ),
                ),
            ],
            options={
                "ordering": ["session", "name", "id"],
            },
        ),
        migrations.CreateModel(
            name="InventoryRawScan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=120)),
                ("current_location_code", models.CharField(blank=True, max_length=32)),
                ("client_scan_id", models.CharField(max_length=64)),
                ("scanned_at", models.DateTimeField()),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "processing_status",
                    models.CharField(
                        choices=[
                            ("received", "Received"),
                            ("processed", "Processed"),
                            ("location", "Location"),
                            ("error", "Error"),
                        ],
                        default="received",
                        max_length=16,
                    ),
                ),
                ("processing_result", models.JSONField(blank=True, default=dict)),
                (
                    "current_location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="inventory_raw_scans",
                        to="locations.location",
                    ),
                ),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="raw_scans",
                        to="inventory.inventorysession",
                    ),
                ),
                (
                    "terminal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="raw_scans",
                        to="inventory.inventoryscanterminal",
                    ),
                ),
            ],
            options={
                "ordering": ["-scanned_at", "-id"],
            },
        ),
        migrations.AddConstraint(
            model_name="inventoryscanterminal",
            constraint=models.UniqueConstraint(
                fields=("session", "name"),
                name="inv_terminal_unique_session_name",
            ),
        ),
        migrations.AddIndex(
            model_name="inventoryscanterminal",
            index=models.Index(fields=["session", "is_active"], name="inv_term_session_active_idx"),
        ),
        migrations.AddConstraint(
            model_name="inventoryrawscan",
            constraint=models.UniqueConstraint(
                fields=("terminal", "client_scan_id"),
                name="inv_raw_scan_unique_terminal_client_id",
            ),
        ),
        migrations.AddIndex(
            model_name="inventoryrawscan",
            index=models.Index(fields=["session", "terminal"], name="inv_raw_session_term_idx"),
        ),
        migrations.AddIndex(
            model_name="inventoryrawscan",
            index=models.Index(fields=["session", "code"], name="inv_raw_scan_session_code_idx"),
        ),
        migrations.AddIndex(
            model_name="inventoryrawscan",
            index=models.Index(fields=["session", "processing_status"], name="inv_raw_session_status_idx"),
        ),
    ]

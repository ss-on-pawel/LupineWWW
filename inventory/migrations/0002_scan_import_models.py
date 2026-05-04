import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("assets", "0008_migrate_asset_type_values"),
        ("inventory", "0001_initial"),
        ("locations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryScanBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("raw_text", models.TextField()),
                ("total_lines", models.PositiveIntegerField(default=0)),
                ("processed_lines", models.PositiveIntegerField(default=0)),
                ("recognized_assets_count", models.PositiveIntegerField(default=0)),
                ("unknown_codes_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scan_batches", to="inventory.inventorysession")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_scan_batches", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="InventoryObservedItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=120)),
                ("status", models.CharField(choices=[("found_ok", "Found OK"), ("found_other_location", "Found other location"), ("found_out_of_scope", "Found out of scope"), ("unknown_code", "Unknown code")], max_length=32)),
                ("first_seen_at", models.DateTimeField()),
                ("last_seen_at", models.DateTimeField()),
                ("asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_observed_items", to="assets.asset")),
                ("scanned_location", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_observed_items", to="locations.location")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="observed_items", to="inventory.inventorysession")),
            ],
            options={
                "ordering": ["code", "id"],
                "indexes": [
                    models.Index(fields=["session", "status"], name="inv_obs_session_status_idx"),
                    models.Index(fields=["session", "code"], name="inv_obs_session_code_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(condition=models.Q(("asset__isnull", False)), fields=("session", "asset"), name="inv_observed_unique_asset_per_session"),
                ],
            },
        ),
    ]

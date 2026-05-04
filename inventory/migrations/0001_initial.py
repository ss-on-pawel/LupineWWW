# Generated manually for the inventory foundation.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("assets", "0008_migrate_asset_type_values"),
        ("locations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="InventorySession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.CharField(max_length=16, unique=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("closed", "Closed")], default="active", max_length=16)),
                ("asset_type_scope", models.JSONField(default=list)),
                ("started_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inventory_sessions", to=settings.AUTH_USER_MODEL)),
                ("scope_root_locations", models.ManyToManyField(blank=True, related_name="inventory_sessions", to="locations.location")),
            ],
            options={
                "ordering": ["-started_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="InventorySnapshotItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("asset_id_snapshot", models.PositiveBigIntegerField()),
                ("inventory_number", models.CharField(max_length=100)),
                ("name", models.CharField(max_length=255)),
                ("barcode", models.CharField(blank=True, max_length=120)),
                ("asset_type", models.CharField(blank=True, max_length=32)),
                ("asset_type_display", models.CharField(blank=True, max_length=120)),
                ("location_fk_id_snapshot", models.PositiveBigIntegerField()),
                ("location_code", models.CharField(blank=True, max_length=32)),
                ("location_name", models.CharField(blank=True, max_length=255)),
                ("location_path", models.CharField(blank=True, max_length=1024)),
                ("status_snapshot", models.CharField(blank=True, max_length=30)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_snapshot_items", to="assets.asset")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="snapshot_items", to="inventory.inventorysession")),
            ],
            options={
                "ordering": ["inventory_number", "id"],
                "indexes": [
                    models.Index(fields=["session", "asset_id_snapshot"], name="inv_snap_session_asset_idx"),
                    models.Index(fields=["session", "location_fk_id_snapshot"], name="inv_snap_session_loc_idx"),
                    models.Index(fields=["session", "asset_type"], name="inv_snap_session_type_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("session", "asset_id_snapshot"), name="inv_snapshot_unique_asset_per_session"),
                ],
            },
        ),
    ]

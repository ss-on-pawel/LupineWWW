import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0010_asset_asset_type_ref"),
        ("inventory", "0002_scan_import_models"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InventorySessionManualQuantity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inventory_manual_quantities", to="assets.asset")),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="manual_quantities", to="inventory.inventorysession")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_manual_quantity_updates", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("session", "asset"), name="inv_manual_qty_unique_session_asset"),
                    models.CheckConstraint(condition=models.Q(("quantity__gte", 0)), name="inv_manual_qty_non_negative"),
                ],
            },
        ),
    ]

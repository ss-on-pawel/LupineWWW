import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0011_asset_record_quantity"),
        ("inventory", "0003_inventorysessionmanualquantity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InventorySessionManualConfirmation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("confirmed_at", models.DateTimeField(auto_now=True)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inventory_manual_confirmations", to="assets.asset")),
                ("confirmed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_manual_confirmations", to=settings.AUTH_USER_MODEL)),
                ("session", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="manual_confirmations", to="inventory.inventorysession")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("session", "asset"), name="inv_manual_conf_unique_session_asset"),
                ],
            },
        ),
    ]

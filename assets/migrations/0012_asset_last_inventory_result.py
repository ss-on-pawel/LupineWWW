from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0005_inventorysnapshotitem_snapshot_quantity_value"),
        ("assets", "0011_asset_record_quantity"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="last_inventory_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Data naniesienia ostatniej inwentaryzacji",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="last_inventory_quantity",
            field=models.PositiveIntegerField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Ilość z ostatniej inwentaryzacji",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="last_inventory_session",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="applied_assets",
                to="inventory.inventorysession",
                verbose_name="Sesja ostatniej inwentaryzacji",
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0004_inventorysessionmanualconfirmation"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorysnapshotitem",
            name="purchase_value_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="inventorysnapshotitem",
            name="record_quantity_snapshot",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]

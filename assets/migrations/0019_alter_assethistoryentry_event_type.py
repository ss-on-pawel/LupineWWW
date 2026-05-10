from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0018_simplify_asset_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="assethistoryentry",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("updated", "Updated"),
                    ("moved", "Moved"),
                    ("inventory_applied", "Inventory applied"),
                    ("withdrawn", "Withdrawn"),
                    ("restored", "Restored"),
                ],
                max_length=50,
                verbose_name="Typ zdarzenia",
            ),
        ),
    ]

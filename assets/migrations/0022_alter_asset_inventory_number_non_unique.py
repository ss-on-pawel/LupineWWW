from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0021_assetbarcodesequence"),
    ]

    operations = [
        migrations.AlterField(
            model_name="asset",
            name="inventory_number",
            field=models.CharField(max_length=100, verbose_name="Numer inwentarzowy"),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0020_assettypedictionary_barcode_prefix"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetBarcodeSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("prefix", models.CharField(max_length=3, verbose_name="Prefix")),
                ("year", models.PositiveSmallIntegerField(verbose_name="Rok")),
                ("next_number", models.PositiveIntegerField(default=1, verbose_name="Następny numer")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Data utworzenia")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Data aktualizacji")),
            ],
            options={
                "verbose_name": "Licznik kodów kreskowych",
                "verbose_name_plural": "Liczniki kodów kreskowych",
                "ordering": ["prefix", "year"],
            },
        ),
        migrations.AddConstraint(
            model_name="assetbarcodesequence",
            constraint=models.UniqueConstraint(
                fields=("prefix", "year"),
                name="asset_barcode_seq_unique_prefix_year",
            ),
        ),
    ]

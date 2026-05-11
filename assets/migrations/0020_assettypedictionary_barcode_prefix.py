from django.db import migrations, models


DEFAULT_BARCODE_PREFIXES = {
    "fixed": "ST",
    "low_value": "WN",
    "intangible": "WP",
    "quantity": "IL",
    "other": "IN",
}


def set_default_barcode_prefixes(apps, schema_editor):
    AssetTypeDictionary = apps.get_model("assets", "AssetTypeDictionary")
    for code, prefix in DEFAULT_BARCODE_PREFIXES.items():
        AssetTypeDictionary.objects.filter(code=code).update(barcode_prefix=prefix)


def unset_default_barcode_prefixes(apps, schema_editor):
    AssetTypeDictionary = apps.get_model("assets", "AssetTypeDictionary")
    AssetTypeDictionary.objects.filter(code__in=DEFAULT_BARCODE_PREFIXES).update(barcode_prefix="")


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0019_alter_assethistoryentry_event_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="assettypedictionary",
            name="barcode_prefix",
            field=models.CharField(blank=True, max_length=3, verbose_name="Prefix kodu"),
        ),
        migrations.RunPython(set_default_barcode_prefixes, unset_default_barcode_prefixes),
    ]

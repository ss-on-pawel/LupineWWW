import django.db.models.deletion
from django.db import migrations, models


def backfill_asset_type_ref(apps, schema_editor):
    Asset = apps.get_model("assets", "Asset")
    AssetTypeDictionary = apps.get_model("assets", "AssetTypeDictionary")

    asset_types_by_code = {
        asset_type.code: asset_type.pk
        for asset_type in AssetTypeDictionary.objects.all().only("pk", "code")
    }
    fallback_asset_type_id = asset_types_by_code.get("other")

    updates = []
    queryset = (
        Asset.objects
        .filter(asset_type_ref__isnull=True)
        .only("pk", "asset_type", "asset_type_ref")
        .order_by("pk")
    )
    for asset in queryset.iterator(chunk_size=500):
        asset_type_ref_id = asset_types_by_code.get(asset.asset_type) or fallback_asset_type_id
        if asset_type_ref_id is None:
            continue
        asset.asset_type_ref_id = asset_type_ref_id
        updates.append(asset)

        if len(updates) >= 500:
            Asset.objects.bulk_update(updates, ["asset_type_ref"], batch_size=500)
            updates = []

    if updates:
        Asset.objects.bulk_update(updates, ["asset_type_ref"], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0009_assettypedictionary"),
    ]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="asset_type_ref",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assets",
                to="assets.assettypedictionary",
                verbose_name="Rodzaj (słownik)",
            ),
        ),
        migrations.RunPython(backfill_asset_type_ref, migrations.RunPython.noop),
    ]

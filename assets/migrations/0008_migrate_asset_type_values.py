from django.db import migrations


def migrate_asset_type_values(apps, schema_editor):
    Asset = apps.get_model("assets", "Asset")
    value_map = {
        "fixed_asset": "fixed",
        "low_value_asset": "low_value",
        "it_equipment": "low_value",
        "other": "other",
    }
    valid_values = {"", "fixed", "low_value", "intangible", "quantity", "other"}
    updates_by_value = {}

    for asset in Asset.objects.all().only("pk", "asset_type"):
        current_value = asset.asset_type or ""
        if current_value == "":
            continue
        new_value = value_map.get(current_value)
        if new_value is None and current_value not in valid_values:
            new_value = "other"
        if new_value is None or new_value == current_value:
            continue
        updates_by_value.setdefault(new_value, []).append(asset.pk)

    for new_value, asset_ids in updates_by_value.items():
        Asset.objects.filter(pk__in=asset_ids).update(asset_type=new_value)


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0007_alter_asset_asset_type"),
    ]

    operations = [
        migrations.RunPython(migrate_asset_type_values, migrations.RunPython.noop),
    ]

from django.db import migrations, models


STATUS_MAP = {
    "in_stock": "active",
    "in_use": "active",
    "reserved": "active",
    "in_service": "inactive",
    "liquidated": "liquidated",
    "sold": "liquidated",
    "lost": "liquidated",
}


def simplify_asset_statuses(apps, schema_editor):
    Asset = apps.get_model("assets", "Asset")
    for old_status, new_status in STATUS_MAP.items():
        Asset.objects.filter(status=old_status).update(status=new_status)

    Asset.objects.filter(status__in=["active", "inactive"]).update(is_active=True)
    Asset.objects.filter(status="liquidated").update(is_active=False)

    try:
        InventorySnapshotItem = apps.get_model("inventory", "InventorySnapshotItem")
    except LookupError:
        return

    for old_status, new_status in STATUS_MAP.items():
        InventorySnapshotItem.objects.filter(status_snapshot=old_status).update(status_snapshot=new_status)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0006_inventorysession_applied_to_assets"),
        ("assets", "0017_asset_current_quantity"),
    ]

    operations = [
        migrations.RunPython(simplify_asset_statuses, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="asset",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Aktywny"),
                    ("inactive", "Nieaktywny"),
                    ("liquidated", "Zlikwidowany"),
                ],
                db_index=True,
                default="active",
                max_length=30,
                verbose_name="Status",
            ),
        ),
    ]

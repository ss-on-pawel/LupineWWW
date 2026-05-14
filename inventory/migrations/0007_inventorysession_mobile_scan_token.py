import secrets

from django.db import migrations, models

from inventory.models import _generate_mobile_scan_token


def populate_mobile_scan_tokens(apps, schema_editor):
    InventorySession = apps.get_model("inventory", "InventorySession")
    for session in InventorySession.objects.all():
        session.mobile_scan_token = secrets.token_urlsafe(48)
        session.save(update_fields=["mobile_scan_token"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0006_inventorysession_applied_to_assets"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorysession",
            name="mobile_scan_token",
            field=models.CharField(default="", editable=False, max_length=64),
        ),
        migrations.RunPython(populate_mobile_scan_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="inventorysession",
            name="mobile_scan_token",
            field=models.CharField(
                default=_generate_mobile_scan_token,
                editable=False,
                max_length=64,
                unique=True,
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0024_assetservicealert"),
    ]

    operations = [
        migrations.AddField(
            model_name="assetattachment",
            name="document_type",
            field=models.CharField(
                blank=True,
                choices=[("lt", "LT — Likwidacja")],
                db_index=True,
                max_length=20,
                verbose_name="Typ dokumentu",
            ),
        ),
        migrations.AddField(
            model_name="assetattachment",
            name="date_of_action",
            field=models.DateField(blank=True, null=True, verbose_name="Data czynności"),
        ),
        migrations.AddField(
            model_name="assetattachment",
            name="is_system_generated",
            field=models.BooleanField(default=False, verbose_name="Wygenerowany przez system"),
        ),
        migrations.AddField(
            model_name="assetattachment",
            name="is_protected",
            field=models.BooleanField(default=False, verbose_name="Chroniony"),
        ),
    ]

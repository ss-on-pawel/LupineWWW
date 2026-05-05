from django.db import migrations, models


DEFAULT_ASSET_TYPES = [
    {
        "code": "fixed",
        "name": "Środek trwały",
        "is_quantity_based": False,
        "sort_order": 10,
        "is_system": True,
    },
    {
        "code": "low_value",
        "name": "Wyposażenie / niskocenne",
        "is_quantity_based": False,
        "sort_order": 20,
        "is_system": True,
    },
    {
        "code": "intangible",
        "name": "WNiP",
        "is_quantity_based": False,
        "sort_order": 30,
        "is_system": True,
    },
    {
        "code": "quantity",
        "name": "Ilościówka",
        "is_quantity_based": True,
        "sort_order": 40,
        "is_system": True,
    },
    {
        "code": "other",
        "name": "Inne",
        "is_quantity_based": False,
        "sort_order": 50,
        "is_system": True,
    },
]


def seed_default_asset_types(apps, schema_editor):
    AssetTypeDictionary = apps.get_model("assets", "AssetTypeDictionary")
    for asset_type in DEFAULT_ASSET_TYPES:
        code = asset_type["code"]
        defaults = {
            "name": asset_type["name"],
            "is_quantity_based": asset_type["is_quantity_based"],
            "is_active": True,
            "sort_order": asset_type["sort_order"],
            "is_system": asset_type["is_system"],
        }
        AssetTypeDictionary.objects.update_or_create(code=code, defaults=defaults)


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0008_migrate_asset_type_values"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetTypeDictionary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="Nazwa")),
                ("code", models.SlugField(max_length=64, unique=True, verbose_name="Kod")),
                ("is_quantity_based", models.BooleanField(default=False, verbose_name="Ilościowy")),
                ("is_active", models.BooleanField(default=True, verbose_name="Aktywny")),
                ("sort_order", models.PositiveIntegerField(default=0, verbose_name="Kolejność")),
                ("is_system", models.BooleanField(default=False, verbose_name="Systemowy")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Data utworzenia")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Data aktualizacji")),
            ],
            options={
                "verbose_name": "Rodzaj środka",
                "verbose_name_plural": "Rodzaje środków",
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.RunPython(seed_default_asset_types, migrations.RunPython.noop),
    ]

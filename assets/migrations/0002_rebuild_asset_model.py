from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


class Migration(migrations.Migration):

    dependencies = [
        ("assets", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameField(
            model_name="asset",
            old_name="added_at",
            new_name="created_at",
        ),
        migrations.AddField(
            model_name="asset",
            name="asset_type",
            field=models.CharField(
                choices=[
                    ("fixed_asset", "Środek trwały"),
                    ("low_value_asset", "Wyposażenie"),
                    ("it_equipment", "Sprzęt IT"),
                    ("software", "Oprogramowanie"),
                    ("vehicle", "Pojazd"),
                    ("other", "Inny składnik"),
                ],
                db_index=True,
                default="fixed_asset",
                max_length=30,
                verbose_name="Typ składnika",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="barcode",
            field=models.CharField(blank=True, max_length=120, verbose_name="Kod kreskowy"),
        ),
        migrations.AddField(
            model_name="asset",
            name="category",
            field=models.CharField(
                db_index=True,
                default="Ogólna",
                max_length=120,
                verbose_name="Kategoria",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="asset",
            name="commissioning_date",
            field=models.DateField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Data przyjęcia do użytkowania",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="cost_center",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=120,
                verbose_name="MPK / centrum kosztów",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="current_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="used_assets",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Aktualny użytkownik",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="department",
            field=models.CharField(blank=True, db_index=True, max_length=120, verbose_name="Dział"),
        ),
        migrations.AddField(
            model_name="asset",
            name="external_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=120,
                verbose_name="Identyfikator zewnętrzny",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="insurance_until",
            field=models.DateField(blank=True, db_index=True, null=True, verbose_name="Ubezpieczenie do"),
        ),
        migrations.AddField(
            model_name="asset",
            name="invoice_number",
            field=models.CharField(blank=True, db_index=True, max_length=120, verbose_name="Numer faktury"),
        ),
        migrations.AddField(
            model_name="asset",
            name="is_active",
            field=models.BooleanField(default=True, verbose_name="Aktywny"),
        ),
        migrations.AddField(
            model_name="asset",
            name="last_inventory_date",
            field=models.DateField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Data ostatniej inwentaryzacji",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="location",
            field=models.CharField(blank=True, db_index=True, max_length=120, verbose_name="Lokalizacja"),
        ),
        migrations.AddField(
            model_name="asset",
            name="manufacturer",
            field=models.CharField(blank=True, db_index=True, max_length=120, verbose_name="Producent"),
        ),
        migrations.AddField(
            model_name="asset",
            name="model",
            field=models.CharField(blank=True, max_length=120, verbose_name="Model"),
        ),
        migrations.AddField(
            model_name="asset",
            name="next_review_date",
            field=models.DateField(blank=True, db_index=True, null=True, verbose_name="Data następnego przeglądu"),
        ),
        migrations.AddField(
            model_name="asset",
            name="organizational_unit",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=120,
                verbose_name="Jednostka organizacyjna",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="purchase_date",
            field=models.DateField(blank=True, db_index=True, null=True, verbose_name="Data zakupu"),
        ),
        migrations.AddField(
            model_name="asset",
            name="purchase_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=12,
                null=True,
                verbose_name="Wartość zakupu",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="responsible_person",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="responsible_assets",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Osoba odpowiedzialna",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="room",
            field=models.CharField(blank=True, max_length=60, verbose_name="Pomieszczenie"),
        ),
        migrations.AddField(
            model_name="asset",
            name="serial_number",
            field=models.CharField(blank=True, db_index=True, max_length=120, verbose_name="Numer seryjny"),
        ),
        migrations.AddField(
            model_name="asset",
            name="status",
            field=models.CharField(
                choices=[
                    ("in_stock", "Na stanie"),
                    ("in_use", "W użyciu"),
                    ("reserved", "Zarezerwowany"),
                    ("in_service", "W serwisie"),
                    ("liquidated", "Zlikwidowany"),
                    ("sold", "Sprzedany"),
                    ("lost", "Utracony"),
                ],
                db_index=True,
                default="in_stock",
                max_length=30,
                verbose_name="Status",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="technical_condition",
            field=models.CharField(
                choices=[
                    ("new", "Nowy"),
                    ("very_good", "Bardzo dobry"),
                    ("good", "Dobry"),
                    ("average", "Średni"),
                    ("poor", "Słaby"),
                    ("damaged", "Uszkodzony"),
                ],
                db_index=True,
                default="good",
                max_length=20,
                verbose_name="Stan techniczny",
            ),
        ),
        migrations.AddField(
            model_name="asset",
            name="updated_at",
            field=models.DateTimeField(default=timezone.now, verbose_name="Data aktualizacji"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="asset",
            name="warranty_until",
            field=models.DateField(blank=True, db_index=True, null=True, verbose_name="Gwarancja do"),
        ),
        migrations.AlterField(
            model_name="asset",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, verbose_name="Data utworzenia"),
        ),
        migrations.AlterField(
            model_name="asset",
            name="description",
            field=models.TextField(blank=True, verbose_name="Opis"),
        ),
        migrations.AlterField(
            model_name="asset",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, verbose_name="Data aktualizacji"),
        ),
        migrations.AlterModelOptions(
            name="asset",
            options={
                "ordering": ["-created_at"],
                "verbose_name": "Składnik majątku",
                "verbose_name_plural": "Składniki majątku",
            },
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["name"], name="asset_name_idx"),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["asset_type", "category"], name="asset_type_cat_idx"),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["status", "technical_condition"], name="asset_status_cond_idx"),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["organizational_unit", "department"], name="asset_org_dept_idx"),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["location", "room"], name="asset_location_room_idx"),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["manufacturer", "model"], name="asset_mfr_model_idx"),
        ),
        migrations.AddIndex(
            model_name="asset",
            index=models.Index(fields=["is_active", "created_at"], name="asset_active_created_idx"),
        ),
        migrations.AddConstraint(
            model_name="asset",
            constraint=models.UniqueConstraint(
                condition=~models.Q(barcode=""),
                fields=("barcode",),
                name="asset_unique_non_empty_barcode",
                violation_error_message="Składnik o tym kodzie kreskowym już istnieje.",
            ),
        ),
    ]

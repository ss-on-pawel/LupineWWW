from django.conf import settings
from django.db import models


class Asset(models.Model):
    class AssetType(models.TextChoices):
        FIXED_ASSET = "fixed_asset", "Środek trwały"
        LOW_VALUE_ASSET = "low_value_asset", "Wyposażenie"
        IT_EQUIPMENT = "it_equipment", "Sprzęt IT"
        SOFTWARE = "software", "Oprogramowanie"
        VEHICLE = "vehicle", "Pojazd"
        OTHER = "other", "Inny składnik"

    class Status(models.TextChoices):
        IN_STOCK = "in_stock", "Na stanie"
        IN_USE = "in_use", "W użyciu"
        RESERVED = "reserved", "Zarezerwowany"
        IN_SERVICE = "in_service", "W serwisie"
        LIQUIDATED = "liquidated", "Zlikwidowany"
        SOLD = "sold", "Sprzedany"
        LOST = "lost", "Utracony"

    class TechnicalCondition(models.TextChoices):
        NEW = "new", "Nowy"
        VERY_GOOD = "very_good", "Bardzo dobry"
        GOOD = "good", "Dobry"
        AVERAGE = "average", "Średni"
        POOR = "poor", "Słaby"
        DAMAGED = "damaged", "Uszkodzony"

    name = models.CharField(max_length=255, verbose_name="Nazwa")
    inventory_number = models.CharField(
        max_length=100,
        unique=True,
        error_messages={
            "unique": "Składnik o tym numerze inwentarzowym już istnieje.",
        },
        verbose_name="Numer inwentarzowy",
    )
    asset_type = models.CharField(
        max_length=30,
        choices=AssetType.choices,
        default=AssetType.FIXED_ASSET,
        db_index=True,
        verbose_name="Typ składnika",
    )
    category = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Kategoria",
    )
    manufacturer = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Producent",
    )
    model = models.CharField(max_length=120, blank=True, verbose_name="Model")
    serial_number = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Numer seryjny",
    )
    barcode = models.CharField(
        max_length=120,
        blank=True,
        verbose_name="Kod kreskowy",
    )
    description = models.TextField(blank=True, verbose_name="Opis")
    purchase_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Data zakupu",
    )
    commissioning_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Data przyjęcia do użytkowania",
    )
    purchase_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Wartość zakupu",
    )
    invoice_number = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Numer faktury",
    )
    external_id = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Identyfikator zewnętrzny",
    )
    cost_center = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="MPK / centrum kosztów",
    )
    organizational_unit = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Jednostka organizacyjna",
    )
    department = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Dział",
    )
    location = models.CharField(
        max_length=120,
        blank=True,
        db_index=True,
        verbose_name="Lokalizacja",
    )
    location_fk = models.ForeignKey(
        "locations.Location",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assets",
        verbose_name="Lokalizacja relacyjna",
    )
    room = models.CharField(max_length=60, blank=True, verbose_name="Pomieszczenie")
    responsible_person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="responsible_assets",
        verbose_name="Osoba odpowiedzialna",
    )
    current_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="used_assets",
        verbose_name="Aktualny użytkownik",
    )
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.IN_STOCK,
        db_index=True,
        verbose_name="Status",
    )
    technical_condition = models.CharField(
        max_length=20,
        choices=TechnicalCondition.choices,
        default=TechnicalCondition.GOOD,
        db_index=True,
        verbose_name="Stan techniczny",
    )
    last_inventory_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Data ostatniej inwentaryzacji",
    )
    next_review_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Data następnego przeglądu",
    )
    warranty_until = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Gwarancja do",
    )
    insurance_until = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Ubezpieczenie do",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktywny")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Data utworzenia")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Data aktualizacji")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Składnik majątku"
        verbose_name_plural = "Składniki majątku"
        indexes = [
            models.Index(fields=["name"], name="asset_name_idx"),
            models.Index(fields=["asset_type", "category"], name="asset_type_cat_idx"),
            models.Index(fields=["status", "technical_condition"], name="asset_status_cond_idx"),
            models.Index(fields=["organizational_unit", "department"], name="asset_org_dept_idx"),
            models.Index(fields=["location", "room"], name="asset_location_room_idx"),
            models.Index(fields=["manufacturer", "model"], name="asset_mfr_model_idx"),
            models.Index(fields=["is_active", "created_at"], name="asset_active_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["barcode"],
                condition=~models.Q(barcode=""),
                name="asset_unique_non_empty_barcode",
                violation_error_message="Składnik o tym kodzie kreskowym już istnieje.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.inventory_number})"

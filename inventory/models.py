from django.conf import settings
from django.db import models
from django.utils import timezone


class InventorySession(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"

    number = models.CharField(max_length=16, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inventory_sessions",
    )
    scope_root_locations = models.ManyToManyField(
        "locations.Location",
        related_name="inventory_sessions",
        blank=True,
    )
    asset_type_scope = models.JSONField(default=list)
    started_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return self.number


class InventorySnapshotItem(models.Model):
    session = models.ForeignKey(
        InventorySession,
        on_delete=models.CASCADE,
        related_name="snapshot_items",
    )
    asset = models.ForeignKey(
        "assets.Asset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_snapshot_items",
    )
    asset_id_snapshot = models.PositiveBigIntegerField()
    inventory_number = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    barcode = models.CharField(max_length=120, blank=True)
    asset_type = models.CharField(max_length=32, blank=True)
    asset_type_display = models.CharField(max_length=120, blank=True)
    location_fk_id_snapshot = models.PositiveBigIntegerField()
    location_code = models.CharField(max_length=32, blank=True)
    location_name = models.CharField(max_length=255, blank=True)
    location_path = models.CharField(max_length=1024, blank=True)
    status_snapshot = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["inventory_number", "id"]
        indexes = [
            models.Index(fields=["session", "asset_id_snapshot"], name="inv_snap_session_asset_idx"),
            models.Index(fields=["session", "location_fk_id_snapshot"], name="inv_snap_session_loc_idx"),
            models.Index(fields=["session", "asset_type"], name="inv_snap_session_type_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "asset_id_snapshot"],
                name="inv_snapshot_unique_asset_per_session",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.session.number}: {self.inventory_number}"


class InventoryScanBatch(models.Model):
    session = models.ForeignKey(
        InventorySession,
        on_delete=models.CASCADE,
        related_name="scan_batches",
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_scan_batches",
    )
    raw_text = models.TextField()
    total_lines = models.PositiveIntegerField(default=0)
    processed_lines = models.PositiveIntegerField(default=0)
    recognized_assets_count = models.PositiveIntegerField(default=0)
    unknown_codes_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.session.number}: import {self.pk or '-'}"


class InventoryObservedItem(models.Model):
    class Status(models.TextChoices):
        FOUND_OK = "found_ok", "Found OK"
        FOUND_OTHER_LOCATION = "found_other_location", "Found other location"
        FOUND_OUT_OF_SCOPE = "found_out_of_scope", "Found out of scope"
        UNKNOWN_CODE = "unknown_code", "Unknown code"

    session = models.ForeignKey(
        InventorySession,
        on_delete=models.CASCADE,
        related_name="observed_items",
    )
    asset = models.ForeignKey(
        "assets.Asset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_observed_items",
    )
    code = models.CharField(max_length=120)
    scanned_location = models.ForeignKey(
        "locations.Location",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_observed_items",
    )
    status = models.CharField(max_length=32, choices=Status.choices)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta:
        ordering = ["code", "id"]
        indexes = [
            models.Index(fields=["session", "status"], name="inv_obs_session_status_idx"),
            models.Index(fields=["session", "code"], name="inv_obs_session_code_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "asset"],
                condition=models.Q(asset__isnull=False),
                name="inv_observed_unique_asset_per_session",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.session.number}: {self.code}"

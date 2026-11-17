"""Historical organizational links; assets keep their identity and source fields."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class AssetCollection(models.Model):
    class Kind(models.TextChoices):
        SET = "set", "Zestaw"
        GROUP = "group", "Grupa środków"

    kind = models.CharField(max_length=8, choices=Kind.choices)
    name = models.CharField(max_length=255)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=["set", "group"]), name="asset_collection_valid_kind"),
            models.CheckConstraint(condition=~models.Q(name=""), name="asset_collection_named"),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="asset_collection_version_positive"),
        ]


class AssetCollectionMembership(models.Model):
    class Role(models.TextChoices):
        PARENT = "parent", "Środek nadrzędny"
        MEMBER = "member", "Element / członek"

    collection = models.ForeignKey(AssetCollection, on_delete=models.PROTECT, related_name="memberships")
    asset = models.ForeignKey("assets.Asset", on_delete=models.PROTECT, related_name="collection_memberships")
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(default=timezone.now)
    joined_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="asset_collection_joins")
    left_at = models.DateTimeField(null=True, blank=True)
    left_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="asset_collection_leaves")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["asset"], condition=models.Q(left_at__isnull=True), name="asset_one_active_collection"),
            models.UniqueConstraint(fields=["collection"], condition=models.Q(left_at__isnull=True, role="parent"), name="asset_collection_one_parent"),
            models.CheckConstraint(condition=models.Q(role__in=["parent", "member"]), name="asset_collection_valid_role"),
            models.CheckConstraint(condition=(models.Q(left_at__isnull=True, left_by__isnull=True) | models.Q(left_at__isnull=False, left_by__isnull=False)), name="asset_collection_leave_complete"),
            models.CheckConstraint(condition=models.Q(left_at__isnull=True) | models.Q(left_at__gte=models.F("joined_at")), name="asset_collection_leave_order"),
        ]


class AssetCollectionEvent(models.Model):
    request_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    collection = models.ForeignKey(AssetCollection, on_delete=models.PROTECT, related_name="events")
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=16)
    payload = models.JSONField(default=dict)
    before = models.JSONField(default=list)
    after = models.JSONField(default=list)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["occurred_at", "pk"]

from django.core.exceptions import ValidationError
from django.db import models


class Location(models.Model):
    name = models.CharField(max_length=255, verbose_name="Nazwa")
    code = models.CharField(
        max_length=32,
        unique=True,
        blank=True,
        editable=False,
        verbose_name="Kod",
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        verbose_name="Lokalizacja nadrzędna",
    )
    is_active = models.BooleanField(default=True, verbose_name="Aktywna")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Data utworzenia")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Data aktualizacji")

    class Meta:
        ordering = ["name", "id"]
        verbose_name = "Lokalizacja"
        verbose_name_plural = "Lokalizacje"
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "name"],
                name="location_unique_name_per_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["parent", "name"], name="location_parent_name_idx"),
            models.Index(fields=["is_active", "name"], name="location_active_name_idx"),
        ]

    def __str__(self) -> str:
        return self.path

    @property
    def parent_name(self) -> str:
        return self.parent.name if self.parent else "—"

    @property
    def path(self) -> str:
        return " / ".join(self.get_ancestors(include_self=True))

    def get_ancestors(self, include_self=False) -> list[str]:
        nodes: list[str] = []
        current = self if include_self else self.parent
        while current is not None:
            nodes.append(current.name)
            current = current.parent
        return list(reversed(nodes))

    def clean(self):
        sibling_query = type(self).objects.filter(name=self.name)
        if self.parent_id is None:
            sibling_query = sibling_query.filter(parent__isnull=True)
        else:
            sibling_query = sibling_query.filter(parent=self.parent)

        if self.pk:
            sibling_query = sibling_query.exclude(pk=self.pk)

        if sibling_query.exists():
            raise ValidationError(
                {"name": "Lokalizacja o tej nazwie już istnieje w tym samym miejscu struktury."}
            )

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if not kwargs.get("update_fields"):
            self.full_clean()
        super().save(*args, **kwargs)
        if is_new and not self.code:
            self.code = f"LOC-{self.pk:06d}"
            super().save(update_fields=["code"])

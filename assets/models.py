from django.db import models


class Asset(models.Model):
    name = models.CharField(max_length=255, verbose_name="Nazwa")
    inventory_number = models.CharField(
        max_length=100,
        unique=True,
        verbose_name="Numer inwentarzowy",
    )
    description = models.TextField(blank=True, verbose_name="Opis")
    added_at = models.DateTimeField(auto_now_add=True, verbose_name="Data dodania")

    class Meta:
        ordering = ["-added_at"]
        verbose_name = "Srodek trwaly"
        verbose_name_plural = "Srodki trwale"

    def __str__(self) -> str:
        return f"{self.name} ({self.inventory_number})"

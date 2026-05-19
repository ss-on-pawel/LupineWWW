"""
Generuje testowe środki trwałe z planami amortyzacji.
Użycie:
    python manage.py seed_depreciation_data            # 300 środków
    python manage.py seed_depreciation_data --count 500
    python manage.py seed_depreciation_data --clear    # usuwa wygenerowane dane
"""
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from assets.models import Asset, AssetDepreciationPlan

SEED_PREFIX = "SEED-DEP-"
SEED_EXTERNAL = "seed_depreciation:"

_BLUEPRINTS = [
    ("Laptop",                  "IT",              Asset.AssetType.LOW_VALUE),
    ("Serwer",                  "Infrastruktura",  Asset.AssetType.FIXED),
    ("Monitor",                 "IT",              Asset.AssetType.LOW_VALUE),
    ("Ploter",                  "Biuro",           Asset.AssetType.LOW_VALUE),
    ("Drukarka laserowa",       "Biuro",           Asset.AssetType.LOW_VALUE),
    ("Klimatyzator",            "Infrastruktura",  Asset.AssetType.FIXED),
    ("Wózek widłowy",           "Magazyn",         Asset.AssetType.FIXED),
    ("Samochód dostawczy",      "Flota",           Asset.AssetType.FIXED),
    ("Frezarka CNC",            "Produkcja",       Asset.AssetType.FIXED),
    ("Agregat prądotwórczy",    "Infrastruktura",  Asset.AssetType.FIXED),
    ("System alarmowy",         "Bezpieczeństwo",  Asset.AssetType.FIXED),
    ("Telefon IP",              "IT",              Asset.AssetType.LOW_VALUE),
    ("Kamera IP",               "Bezpieczeństwo",  Asset.AssetType.LOW_VALUE),
    ("Regał magazynowy",        "Magazyn",         Asset.AssetType.FIXED),
    ("Biurko regulowane",       "Meble",           Asset.AssetType.LOW_VALUE),
    ("Szafa serwerowa",         "Infrastruktura",  Asset.AssetType.FIXED),
    ("Switch sieciowy",         "IT",              Asset.AssetType.FIXED),
    ("Tablica interaktywna",    "AV",              Asset.AssetType.LOW_VALUE),
    ("Skaner dokumentów",       "Biuro",           Asset.AssetType.LOW_VALUE),
    ("UPS rack",                "Infrastruktura",  Asset.AssetType.FIXED),
    ("Tokarka CNC",             "Produkcja",       Asset.AssetType.FIXED),
    ("Sprężarka powietrza",     "Produkcja",       Asset.AssetType.FIXED),
    ("Winda towarowa",          "Infrastruktura",  Asset.AssetType.FIXED),
    ("Kasa fiskalna",           "Biuro",           Asset.AssetType.LOW_VALUE),
    ("Terminal POS",            "IT",              Asset.AssetType.LOW_VALUE),
]

_KST = [
    "3 — Kotły i maszyny energetyczne",
    "4 — Maszyny ogólnego zastosowania",
    "5 — Specjalistyczne maszyny",
    "6 — Urządzenia techniczne",
    "7 — Środki transportu",
    "8 — Narzędzia i przyrządy",
    "10 — Sprzęt komputerowy i peryferyjny",
    "11 — Wyposażenie biurowe",
]

_LOCATIONS = [
    "Warszawa / Centrala",
    "Warszawa / Magazyn A",
    "Kraków / Biuro",
    "Poznań / Oddział",
    "Gdańsk / Serwerownia",
    "Wrocław / Produkcja",
    "Łódź / Logistyka",
    "Katowice / Magazyn",
]

_LINEAR_RATES = [
    Decimal("5.00"),
    Decimal("7.00"),
    Decimal("10.00"),
    Decimal("14.00"),
    Decimal("20.00"),
    Decimal("25.00"),
    Decimal("30.00"),
]

_USEFUL_LIFE = [None, None, None, 36, 60, 84, 120, 180, 240]


def _rand_start_date(rng) -> date:
    today = date.today()
    buckets = [
        # weight, days_ago_min, days_ago_max
        (30, 365 * 6, 365 * 9),   # 6–9 lat temu — dobrze zamortyzowane
        (25, 365 * 3, 365 * 6),   # 3–6 lat temu
        (20, 365 * 1, 365 * 3),   # 1–3 lata temu
        (15, 30,      365),        # do roku
        (10, 1,       30),         # ostatni miesiąc — niemal brak umorzenia
    ]
    bucket = rng.choices(buckets, weights=[b[0] for b in buckets], k=1)[0]
    days_ago = rng.randint(bucket[1], bucket[2])
    d = today - timedelta(days=days_ago)
    return d.replace(day=1)  # zawsze 1. dzień miesiąca


class Command(BaseCommand):
    help = "Generuje testowe środki trwałe z planami amortyzacji."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=300)
        parser.add_argument("--clear", action="store_true",
                            help="Usuwa wcześniej wygenerowane rekordy.")

    def handle(self, *args, **options):
        count = max(options["count"], 0)

        with transaction.atomic():
            if options["clear"]:
                assets = Asset.objects.filter(external_id__startswith=SEED_EXTERNAL)
                pks = list(assets.values_list("pk", flat=True))
                AssetDepreciationPlan.objects.filter(asset_id__in=pks).delete()
                deleted, _ = assets.delete()
                self.stdout.write(self.style.WARNING(f"Usunięto {deleted} środków testowych."))

            if count == 0:
                return

            created = self._seed(count)
            self.stdout.write(self.style.SUCCESS(f"Utworzono {created} środków z planami amortyzacji."))

    def _seed(self, count: int) -> int:
        rng = random.Random()
        seq = self._next_seq()
        assets = []
        plans = []

        for i in range(count):
            name, category, asset_type = rng.choice(_BLUEPRINTS)
            inv = f"{SEED_PREFIX}{seq + i:08d}"

            initial = Decimal(str(rng.randint(500, 150000))) + rng.choice(
                [Decimal("0"), Decimal("0.99"), Decimal("0.49")]
            )
            residual = Decimal("0")
            if rng.random() < 0.25:
                residual = (initial * rng.choice([Decimal("0.05"), Decimal("0.10")])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            start_date = _rand_start_date(rng)
            method = (
                AssetDepreciationPlan.Method.LINEAR
                if rng.random() < 0.72
                else AssetDepreciationPlan.Method.ONE_TIME
            )
            rate = rng.choice(_LINEAR_RATES) if method == AssetDepreciationPlan.Method.LINEAR else None
            useful_life = rng.choice(_USEFUL_LIFE)
            kst = rng.choice(_KST)

            a = Asset(
                name=f"{name} {seq + i:05d}",
                inventory_number=inv,
                barcode=f"DEP-{seq + i:010d}",
                asset_type=asset_type,
                category=category,
                location=rng.choice(_LOCATIONS),
                status=Asset.Status.ACTIVE,
                is_active=True,
                purchase_value=initial,
                commissioning_date=start_date,
                purchase_date=start_date - timedelta(days=rng.randint(0, 30)),
                external_id=f"{SEED_EXTERNAL}{seq + i}",
            )
            assets.append(a)

            plan = AssetDepreciationPlan(
                enabled=True,
                method=method,
                kst_category=kst,
                initial_value=initial,
                residual_value=residual if residual else None,
                depreciation_start_date=start_date,
                annual_rate_percent=rate,
                useful_life_months=useful_life,
            )
            plans.append(plan)

        Asset.objects.bulk_create(assets)

        created_assets = list(
            Asset.objects.filter(
                inventory_number__in=[a.inventory_number for a in assets]
            ).values_list("id", "inventory_number")
        )
        inv_to_id = {inv: pk for pk, inv in created_assets}

        for plan, asset in zip(plans, assets):
            plan.asset_id = inv_to_id[asset.inventory_number]
            plan.monthly_depreciation_amount, plan.annual_depreciation_amount = (
                plan.calculate_depreciation_amounts()
            )

        AssetDepreciationPlan.objects.bulk_create(plans)
        return len(created_assets)

    def _next_seq(self) -> int:
        last = (
            Asset.objects.filter(inventory_number__startswith=SEED_PREFIX)
            .order_by("-inventory_number")
            .values_list("inventory_number", flat=True)
            .first()
        )
        if not last:
            return 1
        try:
            return int(last.replace(SEED_PREFIX, "")) + 1
        except ValueError:
            return 1

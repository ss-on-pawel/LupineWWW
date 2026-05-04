import random
import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assets.models import Asset


SEED_PREFIX = "SEED-AST-"
SEED_EXTERNAL_PREFIX = "seed_asset:"
DEFAULT_BATCH_SIZE = 1000

ASSET_BLUEPRINTS = [
    ("Laptop", "Dell", ["Latitude 5440", "Latitude 7440", "Precision 3580"], "IT"),
    ("Monitor", "LG", ["27BP55U", "24MP400", "34WN80C"], "IT"),
    ("Drukarka", "HP", ["LaserJet Pro 4002", "Color Laser 179", "OfficeJet 9022"], "Biuro"),
    ("Biurko", "Nowy Styl", ["NS-140", "NS-Pro", "ErgoDesk"], "Meble"),
    ("Krzeslo", "Profim", ["Accis Pro", "LightUp", "Motto"], "Meble"),
    ("Telefon", "Samsung", ["Galaxy XCover", "Galaxy S24", "Galaxy A55"], "Mobilne"),
    ("Router", "Cisco", ["RV340", "CBS110", "Meraki GX50"], "Siec"),
    ("Serwer", "Dell EMC", ["PowerEdge T150", "R550", "T350"], "Infrastruktura"),
    ("Projektor", "Epson", ["EB-FH52", "CO-FH02", "EB-982W"], "AV"),
    ("Skaner", "Brother", ["ADS-2200", "DS-940DW", "MFC-L3770"], "Biuro"),
    ("Tablet", "Apple", ["iPad 10", "iPad Air", "iPad Mini"], "Mobilne"),
    ("Wozek magazynowy", "Jungheinrich", ["EJE 116", "AM 22", "ERC 214"], "Magazyn"),
]

LOCATION_POOL = [
    "Warszawa / Centrala",
    "Warszawa / Magazyn A",
    "Krakow / Biuro",
    "Poznan / Oddzial",
    "Gdansk / Serwerownia",
    "Wroclaw / Biuro",
    "Lodz / Produkcja",
    "Katowice / Logistyka",
]

ROOM_POOL = ["A-101", "A-204", "B-015", "B-220", "C-310", "MAG-01", "SRV-02", ""]
UNIT_POOL = ["Finanse", "IT", "Administracja", "Operacje", "Logistyka", "Sprzedaz"]
DEPARTMENT_POOL = ["Back Office", "Helpdesk", "Zakupy", "Kontroling", "Magazyn", "Utrzymanie"]
STATUS_WEIGHTS = [
    (Asset.Status.IN_USE, 40),
    (Asset.Status.IN_STOCK, 18),
    (Asset.Status.RESERVED, 8),
    (Asset.Status.IN_SERVICE, 10),
    (Asset.Status.LIQUIDATED, 6),
    (Asset.Status.SOLD, 6),
    (Asset.Status.LOST, 2),
]
CONDITION_WEIGHTS = [
    (Asset.TechnicalCondition.NEW, 10),
    (Asset.TechnicalCondition.VERY_GOOD, 28),
    (Asset.TechnicalCondition.GOOD, 40),
    (Asset.TechnicalCondition.AVERAGE, 15),
    (Asset.TechnicalCondition.POOR, 5),
    (Asset.TechnicalCondition.DAMAGED, 2),
]
ASSET_TYPE_BY_CATEGORY = {
    "IT": Asset.AssetType.LOW_VALUE,
    "Biuro": Asset.AssetType.LOW_VALUE,
    "Meble": Asset.AssetType.LOW_VALUE,
    "Mobilne": Asset.AssetType.LOW_VALUE,
    "Siec": Asset.AssetType.LOW_VALUE,
    "Infrastruktura": Asset.AssetType.FIXED,
    "AV": Asset.AssetType.LOW_VALUE,
    "Magazyn": Asset.AssetType.FIXED,
}


class Command(BaseCommand):
    help = "Generuje testowe rekordy Asset do testow wydajnosci i UX widoku ewidencji."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1000, help="Liczba rekordow do utworzenia.")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Usuwa wczesniej wygenerowane rekordy testowe przed generowaniem nowych.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help="Rozmiar batcha dla bulk_create.",
        )

    def handle(self, *args, **options):
        start_time = time.perf_counter()
        count = max(options["count"], 0)
        batch_size = max(options["batch_size"], 100)
        clear = options["clear"]

        created_count = 0
        skipped_count = 0

        with transaction.atomic():
            if clear:
                deleted_count, _ = Asset.objects.filter(external_id__startswith=SEED_EXTERNAL_PREFIX).delete()
                self.stdout.write(self.style.WARNING(f"Usunieto {deleted_count} wygenerowanych rekordow testowych."))

            if count == 0:
                duration = time.perf_counter() - start_time
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Zakonczono. Utworzono: 0, pominieto: 0, czas: {duration:.2f}s."
                    )
                )
                return

            created_count, skipped_count = self._seed_assets(count=count, batch_size=batch_size)

        duration = time.perf_counter() - start_time
        self.stdout.write(
            self.style.SUCCESS(
                f"Zakonczono. Utworzono: {created_count}, pominieto: {skipped_count}, czas: {duration:.2f}s."
            )
        )

    def _seed_assets(self, *, count, batch_size):
        rng = random.Random()
        now = timezone.now()
        users = list(get_user_model().objects.order_by("id")[:50])

        start_sequence = self._next_seed_sequence()
        end_sequence = start_sequence + count
        inventory_numbers = [f"{SEED_PREFIX}{sequence:08d}" for sequence in range(start_sequence, end_sequence)]
        existing_inventory_numbers = set(
            Asset.objects.filter(inventory_number__in=inventory_numbers).values_list("inventory_number", flat=True)
        )

        batch_tag = timezone.now().strftime("%Y%m%d%H%M%S")
        assets_to_create = []
        timestamps = {}
        skipped_count = 0

        for offset, sequence in enumerate(range(start_sequence, end_sequence), start=1):
            inventory_number = f"{SEED_PREFIX}{sequence:08d}"
            if inventory_number in existing_inventory_numbers:
                skipped_count += 1
                continue

            asset = self._build_asset(
                rng=rng,
                sequence=sequence,
                ordinal=offset,
                inventory_number=inventory_number,
                now=now,
                batch_tag=batch_tag,
                users=users,
            )
            assets_to_create.append(asset)
            timestamps[inventory_number] = (asset.created_at, asset.updated_at)

        if not assets_to_create:
            return 0, skipped_count

        created_inventory_numbers = [asset.inventory_number for asset in assets_to_create]

        for batch_start in range(0, len(assets_to_create), batch_size):
            Asset.objects.bulk_create(assets_to_create[batch_start:batch_start + batch_size], batch_size=batch_size)

        created_assets = list(
            Asset.objects.filter(inventory_number__in=created_inventory_numbers).only("id", "inventory_number", "created_at", "updated_at")
        )

        for asset in created_assets:
            created_at, updated_at = timestamps[asset.inventory_number]
            asset.created_at = created_at
            asset.updated_at = updated_at

        Asset.objects.bulk_update(created_assets, ["created_at", "updated_at"], batch_size=batch_size)
        return len(created_assets), skipped_count

    def _build_asset(self, *, rng, sequence, ordinal, inventory_number, now, batch_tag, users):
        base_name, manufacturer, models, category = rng.choice(ASSET_BLUEPRINTS)
        model_name = rng.choice(models)
        status = weighted_choice(rng, STATUS_WEIGHTS)
        technical_condition = weighted_choice(rng, CONDITION_WEIGHTS)
        location = rng.choice(LOCATION_POOL)
        room = rng.choice(ROOM_POOL)
        organizational_unit = maybe_blank(rng, rng.choice(UNIT_POOL), blank_probability=0.18)
        department = maybe_blank(rng, rng.choice(DEPARTMENT_POOL), blank_probability=0.22)

        purchase_date = now.date() - timedelta(days=rng.randint(20, 2600))
        commissioning_date = purchase_date + timedelta(days=rng.randint(0, 45))
        last_inventory_date = commissioning_date + timedelta(days=rng.randint(60, 720))
        next_review_date = last_inventory_date + timedelta(days=rng.randint(120, 540))
        warranty_until = purchase_date + timedelta(days=rng.randint(365, 1460))
        insurance_until = purchase_date + timedelta(days=rng.randint(180, 1095))

        created_at = now - timedelta(days=rng.randint(5, 1400), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        updated_at = created_at + timedelta(days=rng.randint(0, 320), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        if updated_at > now:
            updated_at = now - timedelta(minutes=rng.randint(0, 120))

        purchase_value = Decimal(str(rng.randint(450, 125000))) + Decimal(str(rng.choice([0, 0.99, 0.49, 0.75])))
        assigned_person = rng.choice(users) if users and rng.random() < 0.32 else None
        current_user = rng.choice(users) if users and status == Asset.Status.IN_USE and rng.random() < 0.45 else None

        asset = Asset(
            name=f"{base_name} {ordinal:05d}",
            inventory_number=inventory_number,
            asset_type=ASSET_TYPE_BY_CATEGORY.get(category, Asset.AssetType.OTHER),
            category=category if rng.random() > 0.05 else "",
            manufacturer=maybe_blank(rng, manufacturer, blank_probability=0.12),
            model=maybe_blank(rng, model_name, blank_probability=0.1),
            serial_number=maybe_blank(rng, f"SN-{sequence:08d}", blank_probability=0.18),
            barcode=maybe_blank(rng, f"BC-{sequence:010d}", blank_probability=0.42),
            description=maybe_blank(
                rng,
                f"Rekord testowy seed_assets / {base_name.lower()} / partia {batch_tag}.",
                blank_probability=0.55,
            ),
            purchase_date=purchase_date if rng.random() > 0.08 else None,
            commissioning_date=commissioning_date if rng.random() > 0.12 else None,
            purchase_value=purchase_value if rng.random() > 0.06 else None,
            invoice_number=maybe_blank(rng, f"FV/{purchase_date.year}/{sequence:06d}", blank_probability=0.4),
            external_id=f"{SEED_EXTERNAL_PREFIX}{batch_tag}:{sequence}",
            cost_center=maybe_blank(rng, f"MPK-{rng.randint(100, 999)}", blank_probability=0.28),
            organizational_unit=organizational_unit,
            department=department,
            location=location if rng.random() > 0.04 else "",
            room=room,
            responsible_person=assigned_person,
            current_user=current_user,
            status=status,
            technical_condition=technical_condition,
            last_inventory_date=last_inventory_date if rng.random() > 0.2 else None,
            next_review_date=next_review_date if rng.random() > 0.24 else None,
            warranty_until=warranty_until if rng.random() > 0.18 else None,
            insurance_until=insurance_until if rng.random() > 0.28 else None,
            is_active=status not in {Asset.Status.LIQUIDATED, Asset.Status.SOLD, Asset.Status.LOST},
            created_at=created_at,
            updated_at=updated_at,
        )
        return asset

    def _next_seed_sequence(self):
        last_inventory_number = (
            Asset.objects.filter(inventory_number__startswith=SEED_PREFIX)
            .order_by("-inventory_number")
            .values_list("inventory_number", flat=True)
            .first()
        )
        if not last_inventory_number:
            return 1
        try:
            return int(last_inventory_number.replace(SEED_PREFIX, "")) + 1
        except ValueError:
            return 1


def weighted_choice(rng, weighted_values):
    values = [value for value, _weight in weighted_values]
    weights = [weight for _value, weight in weighted_values]
    return rng.choices(values, weights=weights, k=1)[0]


def maybe_blank(rng, value, *, blank_probability):
    return "" if rng.random() < blank_probability else value

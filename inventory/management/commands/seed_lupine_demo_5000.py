from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from random import Random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from assets.models import Asset, AssetHistoryEntry, AssetTypeDictionary
from inventory.models import (
    InventoryScanBatch,
    InventorySession,
    InventorySessionManualConfirmation,
    InventorySessionManualQuantity,
)
from inventory.services import import_inventory_scan_text, start_inventory_session
from inventory.views import _build_inventory_session_analysis
from locations.models import Location


DEMO_PREFIX = "lupine_demo_5000:"
SESSION_PREFIX = "LUP-DEMO-"
DEFAULT_ASSET_COUNT = 5000
RANDOM_SEED = 3000

ASSET_TYPES = [
    ("fixed", "Srodek trwaly", False, 10, "ST"),
    ("low_value", "Wyposazenie / niskocenne", False, 20, "WN"),
    ("intangible", "WNiP", False, 30, "WP"),
    ("quantity", "Ilosciowka", True, 40, "IL"),
    ("other", "Inne", False, 50, "IN"),
]

CITY_TREE = {
    "Warszawa": ["Centrala", "Centrum Operacyjne", "Magazyn Mazowsze"],
    "Krakow": ["Oddzial Poludnie", "Centrum R&D", "Magazyn Regionalny"],
    "Poznan": ["Oddzial Zachod", "Centrum Szkoleniowe", "Magazyn Zachod"],
    "Wroclaw": ["Centrum Technologiczne", "Oddzial Dolny Slask", "Magazyn Serwisowy"],
    "Gdansk": ["Oddzial Polnoc", "Terminal Operacyjny", "Magazyn Portowy"],
    "Lodz": ["Centrum Finansowe", "Back Office", "Magazyn Centralny"],
}

ROOMS = [
    "Recepcja",
    "Open Space A",
    "Open Space B",
    "Sala Zarzadu",
    "Sala Konferencyjna 1",
    "Sala Konferencyjna 2",
    "Serwerownia",
    "Magazyn IT",
    "Archiwum",
    "Pokoj Projektowy",
]

REGULAR_NAMES = [
    "Laptop Lenovo ThinkPad T14",
    "Laptop Dell Latitude 7440",
    "Laptop HP EliteBook 840",
    "Monitor Dell UltraSharp 27",
    "Monitor LG Ergo 32",
    "Drukarka HP LaserJet Pro",
    "Urzadzenie Brother MFC",
    "Telefon Samsung XCover",
    "Telefon iPhone SE",
    "Projektor Epson EB",
    "Router Cisco ISR",
    "Switch Aruba 24p",
    "Terminal Zebra TC",
    "Skaner Fujitsu ScanSnap",
    "Biurko regulowane",
    "Fotel ergonomiczny",
    "Szafa aktowa",
    "Zestaw wideokonferencyjny",
]

QUANTITY_NAMES = [
    "Krzesla konferencyjne",
    "Zestawy sluchawkowe",
    "Adaptery USB-C",
    "Myszki bezprzewodowe",
    "Klawiatury biurowe",
    "Kable HDMI",
    "Czytniki kart",
    "Tokeny sprzetowe",
    "Lampki biurkowe",
    "Zasilacze awaryjne male",
]


class Command(BaseCommand):
    help = "Tworzy duza baze demo Lupine 3.0 z 5000 srodkow i 5 sesjami inventory."

    def add_arguments(self, parser):
        parser.add_argument("--asset-count", type=int, default=DEFAULT_ASSET_COUNT)
        parser.add_argument(
            "--flush-demo",
            action="store_true",
            help="Usuwa poprzednie dane demo utworzone ta komenda przed ponownym seedem.",
        )

    def handle(self, *args, **options):
        asset_count = max(int(options["asset_count"]), 100)
        with transaction.atomic():
            if options["flush_demo"]:
                self._flush_demo_data()

            asset_types = self._seed_asset_types()
            locations = self._seed_locations()
            users = self._seed_users(locations)
            assets = self._seed_assets(asset_count, asset_types, locations)
            sessions = self._seed_inventory_sessions(users["admin"], users["manager"], locations, asset_types)

        self.stdout.write(self.style.SUCCESS("Seed Lupine demo 5000 zakonczony."))
        self.stdout.write(f"Assety demo: {len(assets)}")
        self.stdout.write(f"Lokalizacje demo: {len(locations)}")
        self.stdout.write(f"Sesje inventory: {len(sessions)}")
        self.stdout.write("Uzytkownicy demo:")
        for user in users.values():
            self.stdout.write(f"  - {user.username} / lupine-demo-123")

    def _flush_demo_data(self):
        InventorySession.objects.filter(number__startswith=SESSION_PREFIX).delete()
        Asset.objects.filter(external_id__startswith=DEMO_PREFIX).delete()

    def _seed_asset_types(self):
        result = {}
        for code, name, is_quantity_based, sort_order, barcode_prefix in ASSET_TYPES:
            asset_type, _created = AssetTypeDictionary.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "barcode_prefix": barcode_prefix,
                    "is_quantity_based": is_quantity_based,
                    "is_active": True,
                    "sort_order": sort_order,
                    "is_system": True,
                },
            )
            result[code] = asset_type
        return result

    def _seed_locations(self):
        locations = {}

        def ensure(name, parent=None):
            location, _created = Location.objects.update_or_create(
                parent=parent,
                name=name,
                defaults={"is_active": True},
            )
            locations[location.path] = location
            return location

        for city, sites in CITY_TREE.items():
            city_node = ensure(city)
            for site in sites:
                site_node = ensure(site, city_node)
                for floor in range(0, 5):
                    floor_node = ensure(f"Poziom {floor}", site_node)
                    for room in ROOMS:
                        ensure(room, floor_node)

        return locations

    def _seed_users(self, locations):
        User = get_user_model()
        specs = {
            "admin": {
                "username": "demo.admin",
                "email": "demo.admin@lupine.local",
                "is_staff": True,
                "is_superuser": True,
                "role": UserProfile.Role.ADMIN,
                "can_approve": True,
                "requires_approval": False,
                "roots": [],
            },
            "manager": {
                "username": "demo.manager",
                "email": "demo.manager@lupine.local",
                "is_staff": True,
                "is_superuser": False,
                "role": UserProfile.Role.MANAGER,
                "can_approve": True,
                "requires_approval": False,
                "roots": ["Warszawa", "Krakow"],
            },
            "user": {
                "username": "demo.user",
                "email": "demo.user@lupine.local",
                "is_staff": False,
                "is_superuser": False,
                "role": UserProfile.Role.USER,
                "can_approve": False,
                "requires_approval": True,
                "roots": ["Poznan"],
            },
        }
        users = {}
        User.objects.filter(username="demo.reviewer").delete()
        for key, spec in specs.items():
            user, _created = User.objects.update_or_create(
                username=spec["username"],
                defaults={
                    "email": spec["email"],
                    "is_staff": spec["is_staff"],
                    "is_superuser": spec["is_superuser"],
                    "is_active": True,
                },
            )
            user.set_password("lupine-demo-123")
            user.save(update_fields=["password", "email", "is_staff", "is_superuser", "is_active"])
            profile = user.profile
            profile.role = spec["role"]
            profile.can_approve_asset_changes = spec["can_approve"]
            profile.asset_changes_require_approval = spec["requires_approval"]
            profile.save(update_fields=["role", "can_approve_asset_changes", "asset_changes_require_approval"])
            profile.allowed_locations.set([locations[root] for root in spec["roots"]])
            users[key] = user
        return users

    def _seed_assets(self, asset_count, asset_types, locations):
        Asset.objects.filter(external_id__startswith=DEMO_PREFIX).delete()
        rng = Random(RANDOM_SEED)
        leaf_locations = [location for path, location in locations.items() if path.count(" / ") == 3]
        statuses = [Asset.Status.ACTIVE, Asset.Status.INACTIVE]
        conditions = [
            Asset.TechnicalCondition.NEW,
            Asset.TechnicalCondition.VERY_GOOD,
            Asset.TechnicalCondition.GOOD,
            Asset.TechnicalCondition.AVERAGE,
        ]
        type_codes = ["fixed", "low_value", "quantity", "intangible", "other"]
        type_weights = [36, 28, 18, 8, 10]
        assets = []
        for index in range(1, asset_count + 1):
            type_code = rng.choices(type_codes, weights=type_weights, k=1)[0]
            asset_type = asset_types[type_code]
            location = leaf_locations[(index * 37 + rng.randrange(len(leaf_locations))) % len(leaf_locations)]
            is_quantity = asset_type.is_quantity_based
            current_quantity = self._quantity_for(index, type_code, rng)
            name = self._asset_name(index, type_code, is_quantity)
            inventory_number = f"LUP-{index:05d}"
            is_archived = index % 47 == 0
            status = Asset.Status.LIQUIDATED if is_archived else rng.choice(statuses)
            assets.append(
                Asset(
                    name=name,
                    inventory_number=inventory_number,
                    asset_type=asset_type.code,
                    asset_type_ref=asset_type,
                    record_quantity=current_quantity,
                    current_quantity=0 if is_archived and index % 2 == 0 else current_quantity,
                    category=self._category_for(type_code),
                    manufacturer=self._manufacturer_for(index, type_code),
                    model=f"Model {100 + index % 900}",
                    serial_number=f"SN-LUP-{index:07d}",
                    barcode=f"BC-LUP-{index:05d}",
                    description="Demo enterprise asset generated for Lupine 3.0 testing.",
                    purchase_value=Decimal(100 + (index * 17) % 25000).quantize(Decimal("0.01")),
                    invoice_number=f"FV/2026/{index:05d}",
                    external_id=f"{DEMO_PREFIX}{inventory_number}",
                    cost_center=f"CC-{100 + index % 40:03d}",
                    organizational_unit=self._org_unit_for(location),
                    department=self._department_for(index),
                    location=location.path,
                    location_fk=location,
                    room=location.name,
                    status=status,
                    technical_condition=rng.choice(conditions),
                    is_active=not is_archived,
                )
            )

        Asset.objects.bulk_create(assets, batch_size=500)
        return list(Asset.objects.filter(external_id__startswith=DEMO_PREFIX).select_related("location_fk", "asset_type_ref"))

    def _quantity_for(self, index, type_code, rng):
        if type_code == "quantity":
            return rng.choice([5, 8, 10, 12, 15, 20, 25, 40, 50, 75, 100, 150, 200])
        if type_code in {"low_value", "other"} and index % 9 == 0:
            return rng.choice([2, 3, 4, 5, 8])
        return 1

    def _asset_name(self, index, type_code, is_quantity):
        if is_quantity:
            base = QUANTITY_NAMES[index % len(QUANTITY_NAMES)]
        else:
            base = REGULAR_NAMES[index % len(REGULAR_NAMES)]
        return f"{base} - pakiet {index % 200:03d}" if is_quantity else f"{base} #{index:05d}"

    def _category_for(self, type_code):
        return {
            "fixed": "IT i infrastruktura",
            "low_value": "Wyposazenie operacyjne",
            "quantity": "Zasoby ilosciowe",
            "intangible": "Licencje i WNiP",
            "other": "Pozostale",
        }[type_code]

    def _manufacturer_for(self, index, type_code):
        pools = {
            "fixed": ["Dell", "Lenovo", "HP", "Cisco", "Epson"],
            "low_value": ["Logitech", "Ikea Business", "Samsung", "Brother"],
            "quantity": ["Office Depot", "Lyreco", "3M", "Targus"],
            "intangible": ["Microsoft", "Adobe", "Atlassian", "JetBrains"],
            "other": ["Lupine Vendor", "Enterprise Supply", "Service Partner"],
        }
        return pools[type_code][index % len(pools[type_code])]

    def _department_for(self, index):
        departments = ["IT", "Finanse", "Operacje", "HR", "Sprzedaz", "Logistyka", "Administracja", "R&D"]
        return departments[index % len(departments)]

    def _org_unit_for(self, location):
        return location.get_ancestors(include_self=True)[0]

    def _seed_inventory_sessions(self, admin_user, reviewer_user, locations, asset_types):
        self._clear_demo_sessions()
        all_type_codes = [asset_type.code for asset_type in asset_types.values()]
        configs = [
            {"number": "LUP-DEMO-01", "status": InventorySession.Status.ACTIVE, "roots": ["Warszawa"], "types": all_type_codes},
            {"number": "LUP-DEMO-02", "status": InventorySession.Status.ACTIVE, "roots": ["Krakow"], "types": ["fixed", "low_value", "quantity"]},
            {"number": "LUP-DEMO-03", "status": InventorySession.Status.ACTIVE, "roots": ["Poznan", "Wroclaw"], "types": all_type_codes},
            {"number": "LUP-DEMO-04", "status": InventorySession.Status.CLOSED, "roots": ["Gdansk"], "types": ["fixed", "quantity", "other"]},
            {"number": "LUP-DEMO-05", "status": InventorySession.Status.CLOSED, "roots": ["Lodz", "Warszawa"], "types": all_type_codes},
        ]
        sessions = []
        for offset, config in enumerate(configs):
            session = start_inventory_session(
                created_by=admin_user,
                root_locations=[locations[root] for root in config["roots"]],
                asset_types=config["types"],
            )
            session.number = config["number"]
            session.started_at = timezone.now() - timezone.timedelta(days=10 - offset * 2)
            session.save(update_fields=["number", "started_at", "updated_at"])
            self._seed_inventory_activity(session, reviewer_user)
            if config["status"] == InventorySession.Status.CLOSED:
                session.status = InventorySession.Status.CLOSED
                session.closed_at = timezone.now() - timezone.timedelta(days=max(1, 8 - offset))
                session.save(update_fields=["status", "closed_at", "updated_at"])
                self._apply_session_to_assets(session, reviewer_user)
            sessions.append(session)
        return sessions

    def _clear_demo_sessions(self):
        InventorySession.objects.filter(number__startswith=SESSION_PREFIX).delete()

    def _seed_inventory_activity(self, session, user):
        snapshot_items = list(
            session.snapshot_items.select_related("asset").order_by("id")[:1400]
        )
        if not snapshot_items:
            return
        wrong_location = self._pick_wrong_location(snapshot_items)
        lines_by_location = defaultdict(list)
        manual_quantities = []
        manual_confirmations = []
        unknown_codes = []

        for index, item in enumerate(snapshot_items):
            asset = item.asset
            if asset is None:
                continue
            is_quantity = item.asset_type == "quantity"
            mode = index % 11
            if is_quantity:
                expected = (
                    item.record_quantity_snapshot
                    if item.record_quantity_snapshot is not None
                    else asset.current_quantity
                )
                actual = self._target_quantity(expected, mode)
                read_count = min(actual, 6 if mode % 3 else 3)
                manual_count = max(actual - read_count, 0)
                if read_count:
                    target_location_id = wrong_location.id if mode == 7 else item.location_fk_id_snapshot
                    lines_by_location[target_location_id].extend([asset.barcode or asset.inventory_number] * read_count)
                if manual_count or mode in {3, 5, 8}:
                    manual_quantities.append(
                        InventorySessionManualQuantity(
                            session=session,
                            asset=asset,
                            quantity=manual_count,
                            updated_by=user,
                        )
                    )
            else:
                if mode in {0, 1, 2, 3, 4, 5}:
                    lines_by_location[item.location_fk_id_snapshot].append(asset.barcode or asset.inventory_number)
                elif mode == 6:
                    lines_by_location[wrong_location.id].append(asset.barcode or asset.inventory_number)
                elif mode == 8:
                    manual_confirmations.append(
                        InventorySessionManualConfirmation(session=session, asset=asset, confirmed_by=user)
                    )

        for i in range(1, 8):
            unknown_codes.append(f"UNKNOWN-{session.number}-{i:03d}")

        raw_lines = [session.number]
        locations_by_id = {location.id: location for location in Location.objects.filter(id__in=lines_by_location)}
        for location_id, codes in lines_by_location.items():
            location = locations_by_id.get(location_id)
            if location is None:
                continue
            raw_lines.append(location.code)
            raw_lines.extend(codes)
        raw_lines.extend(unknown_codes)
        if len(raw_lines) > 1:
            import_inventory_scan_text("\n".join(raw_lines), uploaded_by=user)
        InventorySessionManualQuantity.objects.bulk_create(manual_quantities, ignore_conflicts=True)
        InventorySessionManualConfirmation.objects.bulk_create(manual_confirmations, ignore_conflicts=True)

    def _target_quantity(self, expected, mode):
        if mode in {0, 1, 2}:
            return expected
        if mode in {3, 4, 5}:
            return max(expected - max(1, expected // 4), 0)
        if mode in {6, 7}:
            return expected + max(1, expected // 5)
        if mode == 8:
            return 0
        return max(expected - 1, 0)

    def _pick_wrong_location(self, snapshot_items):
        location_ids = {item.location_fk_id_snapshot for item in snapshot_items}
        location = Location.objects.exclude(id__in=location_ids).filter(is_active=True).order_by("id").last()
        if location is None:
            location = Location.objects.filter(is_active=True).order_by("id").last()
        return location

    def _apply_session_to_assets(self, session, user):
        now = timezone.now()
        analysis = _build_inventory_session_analysis(session)
        assets_to_update = []
        history_entries = []
        for work_item in analysis["inventory_work_items"]:
            asset = work_item["snapshot"].asset
            if asset is None or not asset.is_active:
                continue
            old_quantity = asset.current_quantity
            new_quantity = work_item["actual_quantity"]
            if old_quantity != new_quantity:
                history_entries.append(
                    AssetHistoryEntry(
                        asset=asset,
                        occurred_at=now,
                        operator=user,
                        event_type=AssetHistoryEntry.EventType.INVENTORY_APPLIED,
                        description="Naniesiono wynik inwentaryzacji demo",
                        old_value=str(old_quantity),
                        new_value=str(new_quantity),
                        field_name="current_quantity",
                        source_object_type="InventorySession",
                        source_object_id=session.pk,
                    )
                )
            asset.current_quantity = new_quantity
            asset.last_inventory_quantity = new_quantity
            asset.last_inventory_session = session
            asset.last_inventory_at = now
            asset.updated_at = now
            assets_to_update.append(asset)

        Asset.objects.bulk_update(
            assets_to_update,
            ["current_quantity", "last_inventory_quantity", "last_inventory_session", "last_inventory_at", "updated_at"],
            batch_size=500,
        )
        AssetHistoryEntry.objects.bulk_create(history_entries, batch_size=500)
        session.applied_to_assets_at = now
        session.applied_to_assets_by = user
        session.save(update_fields=["applied_to_assets_at", "applied_to_assets_by", "updated_at"])

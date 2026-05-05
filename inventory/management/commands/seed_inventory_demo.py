from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from assets.models import Asset, AssetTypeDictionary
from inventory.models import (
    InventoryObservedItem,
    InventoryScanBatch,
    InventorySession,
    InventorySessionManualQuantity,
    InventorySnapshotItem,
)
from inventory.services import import_inventory_scan_text
from locations.models import Location


SESSION_NUMBER = "INV-DEMO-001"
DEMO_USER_USERNAME = "inventory-demo-user"
DEMO_ASSET_EXTERNAL_PREFIX = "inventory_demo:"

ASSET_TYPE_SPECS = [
    ("fixed", "Środek trwały", False, 10),
    ("low_value", "Wyposażenie / niskocenne", False, 20),
    ("quantity", "Ilościówka", True, 40),
    ("intangible", "WNiP", False, 30),
]

LOCATION_TREE = {
    "Miasto 001": {
        "Budynek A": {
            "Recepcja": {},
            "Pokój 101": {},
            "Pokój 102": {},
        },
        "Magazyn 1": {
            "Strefa A": {},
            "Strefa B": {},
        },
    },
    "Miasto 002": {
        "Oddział 1": {
            "Recepcja": {},
            "Sekcja A": {},
            "Sekcja B": {},
        },
    },
}

REGULAR_ASSET_NAMES = [
    "Laptop Dell Latitude",
    "Laptop Lenovo ThinkPad",
    "Laptop HP ProBook",
    "Monitor LG 27",
    "Monitor Dell 24",
    "Monitor Samsung 32",
    "Drukarka HP LaserJet",
    "Drukarka Brother MFC",
    "Telefon Samsung XCover",
    "Telefon iPhone SE",
    "Router Cisco",
    "Router MikroTik",
    "Projektor Epson",
    "Projektor BenQ",
    "Skaner Brother",
    "Skaner Canon",
    "Terminal Zebra",
    "Terminal Honeywell",
    "Biurko 140",
    "Biurko regulowane",
    "Szafa metalowa",
    "Szafa aktowa",
    "Laptop MacBook Air",
    "Monitor Eizo",
    "Drukarka Zebra",
    "Telefon VoIP",
    "Router Ubiquiti",
    "Projektor NEC",
    "Skaner Fujitsu",
    "Terminal magazynowy",
]

QUANTITY_ASSET_NAMES = [
    "Krzesła konferencyjne",
    "Kubki",
    "Talerze",
    "Sztućce",
    "Czajniki",
    "Pościel",
    "Ręczniki",
    "Lampki biurkowe",
    "Notesy szkoleniowe",
    "Baterie AA",
]


class Command(BaseCommand):
    help = "Tworzy lokalne dane demo do ręcznego testowania sesji inwentaryzacji."

    def handle(self, *args, **options):
        with transaction.atomic():
            asset_types = self._seed_asset_types()
            locations = self._seed_locations()
            user = self._get_demo_user()
            regular_assets = self._seed_regular_assets(asset_types, locations)
            quantity_assets = self._seed_quantity_assets(asset_types, locations)
            session = self._refresh_session(
                user=user,
                root_locations=[locations["Miasto 001"], locations["Miasto 002"]],
                asset_types=asset_types,
                assets=regular_assets + quantity_assets,
            )
            raw_text = self._build_raw_text(session, locations)
            batch = import_inventory_scan_text(raw_text, uploaded_by=user)
            manual_count = self._seed_manual_quantities(session, quantity_assets, user)

        self.stdout.write(self.style.SUCCESS("Seed demo inwentaryzacji zakończony."))
        self.stdout.write(f"Sesja: {session.number}")
        self.stdout.write(f"Lokalizacje demo: {len(locations)}")
        self.stdout.write(f"Assety zwykłe: {len(regular_assets)}")
        self.stdout.write(f"Assety ilościowe: {len(quantity_assets)}")
        self.stdout.write(f"Batch ID: {batch.id}")
        self.stdout.write(f"Manual quantities: {manual_count}")
        self.stdout.write("Otwórz: /inventory/{}/".format(session.pk))

    def _seed_asset_types(self):
        asset_types = {}
        for code, name, is_quantity_based, sort_order in ASSET_TYPE_SPECS:
            asset_type, _created = AssetTypeDictionary.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "is_quantity_based": is_quantity_based,
                    "is_active": True,
                    "sort_order": sort_order,
                    "is_system": True,
                },
            )
            asset_types[code] = asset_type
        return asset_types

    def _seed_locations(self):
        locations = {}

        def ensure_node(name, parent=None):
            location, _created = Location.objects.update_or_create(
                parent=parent,
                name=name,
                defaults={"is_active": True},
            )
            locations[location.path] = location
            locations[name] = location if parent is None else locations.get(name, location)
            return location

        def walk(tree, parent=None):
            for name, children in tree.items():
                location = ensure_node(name, parent=parent)
                walk(children, parent=location)

        walk(LOCATION_TREE)
        return locations

    def _get_demo_user(self):
        User = get_user_model()
        user, _created = User.objects.get_or_create(
            username=DEMO_USER_USERNAME,
            defaults={
                "email": "inventory-demo@example.com",
                "is_staff": True,
            },
        )
        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        return user

    def _seed_regular_assets(self, asset_types, locations):
        location_cycle = [
            locations["Miasto 001 / Budynek A / Recepcja"],
            locations["Miasto 001 / Budynek A / Pokój 101"],
            locations["Miasto 001 / Budynek A / Pokój 102"],
            locations["Miasto 001 / Magazyn 1 / Strefa A"],
            locations["Miasto 001 / Magazyn 1 / Strefa B"],
            locations["Miasto 002 / Oddział 1 / Recepcja"],
            locations["Miasto 002 / Oddział 1 / Sekcja A"],
            locations["Miasto 002 / Oddział 1 / Sekcja B"],
        ]
        type_cycle = ["fixed", "low_value", "intangible", "low_value"]
        assets = []
        for index, name in enumerate(REGULAR_ASSET_NAMES, start=1):
            inventory_number = f"TEST-AST-{index:04d}"
            location = location_cycle[(index - 1) % len(location_cycle)]
            asset_type = asset_types[type_cycle[(index - 1) % len(type_cycle)]]
            asset, _created = Asset.objects.update_or_create(
                inventory_number=inventory_number,
                defaults={
                    "name": name,
                    "asset_type": asset_type.code,
                    "asset_type_ref": asset_type,
                    "record_quantity": 1,
                    "category": "Demo zwykłe",
                    "barcode": inventory_number,
                    "location": location.path,
                    "location_fk": location,
                    "status": Asset.Status.IN_STOCK,
                    "technical_condition": Asset.TechnicalCondition.GOOD,
                    "external_id": f"{DEMO_ASSET_EXTERNAL_PREFIX}{inventory_number}",
                    "is_active": True,
                },
            )
            assets.append(asset)
        return assets

    def _seed_quantity_assets(self, asset_types, locations):
        location_cycle = [
            locations["Miasto 001 / Magazyn 1 / Strefa A"],
            locations["Miasto 001 / Magazyn 1 / Strefa B"],
            locations["Miasto 001 / Budynek A / Recepcja"],
            locations["Miasto 002 / Oddział 1 / Sekcja A"],
        ]
        quantity_type = asset_types["quantity"]
        record_quantities = [10, 25, 50, 100, 200, 15, 35, 75, 125, 250]
        assets = []
        for index, name in enumerate(QUANTITY_ASSET_NAMES, start=1):
            inventory_number = f"TEST-QTY-{index:04d}"
            location = location_cycle[(index - 1) % len(location_cycle)]
            asset, _created = Asset.objects.update_or_create(
                inventory_number=inventory_number,
                defaults={
                    "name": name,
                    "asset_type": quantity_type.code,
                    "asset_type_ref": quantity_type,
                    "record_quantity": record_quantities[(index - 1) % len(record_quantities)],
                    "category": "Demo ilościowe",
                    "barcode": inventory_number,
                    "location": location.path,
                    "location_fk": location,
                    "status": Asset.Status.IN_STOCK,
                    "technical_condition": Asset.TechnicalCondition.GOOD,
                    "external_id": f"{DEMO_ASSET_EXTERNAL_PREFIX}{inventory_number}",
                    "is_active": True,
                },
            )
            assets.append(asset)
        return assets

    def _refresh_session(self, *, user, root_locations, asset_types, assets):
        InventorySession.objects.filter(number=SESSION_NUMBER).delete()
        session = InventorySession.objects.create(
            number=SESSION_NUMBER,
            created_by=user,
            status=InventorySession.Status.ACTIVE,
            asset_type_scope=[asset_type.code for asset_type in asset_types.values()],
            started_at=timezone.now(),
        )
        session.scope_root_locations.set(root_locations)
        InventorySnapshotItem.objects.bulk_create(
            [self._build_snapshot_item(session, asset) for asset in assets],
            batch_size=200,
        )
        return session

    def _build_snapshot_item(self, session, asset):
        location = asset.location_fk
        return InventorySnapshotItem(
            session=session,
            asset=asset,
            asset_id_snapshot=asset.id,
            inventory_number=asset.inventory_number,
            name=asset.name,
            barcode=asset.barcode,
            asset_type=asset.asset_type,
            asset_type_display=asset.asset_type_ref.name if asset.asset_type_ref else asset.get_asset_type_display(),
            location_fk_id_snapshot=location.id,
            location_code=location.code,
            location_name=location.name,
            location_path=location.path,
            status_snapshot=asset.status,
        )

    def _build_raw_text(self, session, locations):
        return "\n".join(
            [
                session.number,
                locations["Miasto 001 / Budynek A / Recepcja"].code,
                "TEST-AST-0001",
                "TEST-AST-0003",
                "TEST-AST-0009",
                "TEST-AST-0009",
                "TEST-QTY-0003",
                "TEST-QTY-0003",
                locations["Miasto 001 / Magazyn 1 / Strefa A"].code,
                "TEST-QTY-0001",
                "TEST-QTY-0001",
                "TEST-QTY-0001",
                "TEST-QTY-0010",
                "TEST-QTY-0010",
                "TEST-AST-0004",
                locations["Miasto 001 / Magazyn 1 / Strefa B"].code,
                "TEST-QTY-0002",
                "TEST-QTY-0002",
                "TEST-QTY-0002",
                "TEST-QTY-0002",
                "TEST-QTY-0004",
                "TEST-QTY-0004",
                "UNKNOWN-DEMO-001",
                locations["Miasto 002 / Oddział 1 / Sekcja A"].code,
                "TEST-AST-0002",
                "TEST-AST-0005",
                "TEST-QTY-0004",
                "TEST-QTY-0005",
                "TEST-QTY-0005",
                "UNKNOWN-DEMO-002",
                locations["Miasto 002 / Oddział 1 / Sekcja B"].code,
                "TEST-AST-0012",
                "TEST-QTY-0008",
                "TEST-QTY-0008",
                "TEST-QTY-0008",
                "TEST-QTY-0008",
                "TEST-QTY-0008",
                locations["Miasto 002 / Oddział 1 / Recepcja"].code,
                "TEST-AST-0017",
                "TEST-AST-0024",
                "TEST-QTY-0006",
            ]
        )

    def _seed_manual_quantities(self, session, quantity_assets, user):
        manual_values = {
            "TEST-QTY-0001": 0,
            "TEST-QTY-0002": 3,
            "TEST-QTY-0003": 12,
            "TEST-QTY-0004": 5,
        }
        count = 0
        assets_by_number = {asset.inventory_number: asset for asset in quantity_assets}
        for inventory_number, quantity in manual_values.items():
            asset = assets_by_number[inventory_number]
            InventorySessionManualQuantity.objects.update_or_create(
                session=session,
                asset=asset,
                defaults={
                    "quantity": quantity,
                    "updated_by": user,
                },
            )
            count += 1
        return count

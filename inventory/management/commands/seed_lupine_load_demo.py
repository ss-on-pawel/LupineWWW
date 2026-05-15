from __future__ import annotations

from decimal import Decimal
from random import Random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import UserProfile
from assets.models import Asset, AssetServiceAlert, AssetTypeDictionary
from inventory.models import (
    InventoryObservedItem,
    InventorySession,
    InventorySessionManualConfirmation,
    InventorySessionManualQuantity,
)
from inventory.services import start_inventory_session
from locations.models import Location

LOAD_DEMO_PREFIX = "load_demo:"
LOAD_SESSION_PREFIX = "LOAD-"
LOAD_USER_PREFIX = "load_demo_"
LOAD_ROOT_NAME = "LOAD Demo"
DEFAULT_SEED = 42_000

ASSET_TYPES_SPEC = [
    # (code, name, is_quantity_based, sort_order, barcode_prefix)
    ("it_sprzet", "IT – sprzęt", False, 10, "IT"),
    ("wyposazenie", "Wyposażenie biurowe", False, 20, "WB"),
    ("narzedzia", "Narzędzia i urządzenia", False, 30, "NZ"),
    ("licencje", "Licencje i oprogramowanie", False, 40, "LI"),
    ("materialy", "Materiały eksploatacyjne", True, 50, "MA"),
    ("meble", "Meble", False, 60, "MB"),
    ("inne_load", "Inne (load-demo)", False, 70, "IN"),
]

CITY_SITES = {
    "Warszawa": ["Centrala", "Centrum Operacyjne", "Magazyn Mazowsze"],
    "Kraków": ["Oddział Południe", "Centrum R&D", "Magazyn Regionalny"],
    "Poznań": ["Oddział Zachód", "Centrum Szkoleniowe", "Magazyn Zachód"],
    "Wrocław": ["Centrum Technologiczne", "Oddział Dolny Śląsk", "Magazyn Serwisowy"],
    "Gdańsk": ["Oddział Północ", "Terminal Operacyjny", "Magazyn Portowy"],
    "Łódź": ["Centrum Finansowe", "Back Office", "Magazyn Centralny"],
}
FLOORS = ["Piętro 0", "Piętro 1", "Piętro 2"]
ROOMS = [
    "Recepcja", "Open Space A", "Open Space B",
    "Sala Konferencyjna 1", "Sala Konferencyjna 2",
    "Serwerownia", "Magazyn IT", "Pokój Projektowy",
]

ASSET_NAMES = {
    "it_sprzet": [
        "Laptop Lenovo ThinkPad T14", "Laptop Dell Latitude 7440", "Laptop HP EliteBook 840",
        "Stacja robocza Dell OptiPlex", "Serwer HP ProLiant DL380", "Router Cisco ISR 4431",
        "Switch Aruba 24p PoE", "Projektor Epson EB-L200F", "Skaner Fujitsu ScanSnap iX1600",
        "Terminal Zebra TC52", "Drukarka HP LaserJet Pro M404", "Zasilacz UPS APC 1500VA",
    ],
    "wyposazenie": [
        "Monitor Dell UltraSharp 27 U2722D", "Monitor LG Ergo 32BN88U", "Monitor BenQ PD2725U",
        "Telefon Samsung XCover 5", "Telefon iPhone SE 3gen", "Stacja dokująca Dell WD19",
        "Zestaw wideokonferencyjny Poly Studio", "Drukarka etykiet Zebra ZD421", "Tablet iPad 10gen",
    ],
    "narzedzia": [
        "Wkrętarka Bosch GSR 18V", "Multimetr Fluke 117", "Lutownica stacja Hakko FX-951",
        "Szlifierka kątowa Makita GA5030R", "Poziomica laserowa Leica LINO L4P1",
        "Klucz dynamometryczny Gedore 3550-01", "Tester okablowania Fluke Networks",
    ],
    "licencje": [
        "Licencja Microsoft 365 Business", "Licencja Adobe Creative Cloud",
        "Licencja JetBrains All Products Pack", "Licencja Atlassian Jira Software",
        "Oprogramowanie AutoCAD LT 2025", "Licencja antywirusowa Kaspersky Endpoint",
    ],
    "materialy": [
        "Tonery HP LaserJet CF217A zestaw", "Papier A4 80g ryza 5 szt",
        "Etykiety do drukarki Zebra 102x51 zestaw", "Kable HDMI 2.0 zestaw 10 szt",
        "Adaptery USB-C wielofunkcyjne zestaw", "Myszki bezprzewodowe Logitech M750",
        "Klawiatury Dell KB216 zestaw 5 szt", "Zestawy słuchawkowe Jabra Evolve2 30",
    ],
    "meble": [
        "Biurko regulowane elektrycznie Flexispot E2B", "Fotel ergonomiczny Herman Miller Aeron",
        "Szafa aktowa metalowa 4-półkowa", "Regał biurowy 5-półkowy", "Krzesło konferencyjne Kinnarps",
        "Wieszak na ubrania stalowy", "Kontener mobilny pod biurko",
    ],
    "inne_load": [
        "Gaśnica proszkowa ABC 6kg", "Apteczka pierwszej pomocy DIN 13157",
        "Wózek transportowy ręczny 300kg", "Drabina aluminiowa 3m EN131", "Wentylator przemysłowy 50cm",
    ],
}

MANUFACTURERS = {
    "it_sprzet": ["Dell", "Lenovo", "HP", "Cisco", "Aruba", "Epson", "Zebra", "Fujitsu"],
    "wyposazenie": ["Dell", "LG", "Samsung", "Apple", "Poly", "BenQ", "Jabra"],
    "narzedzia": ["Bosch", "Makita", "Fluke", "Hakko", "Leica", "Gedore"],
    "licencje": ["Microsoft", "Adobe", "JetBrains", "Atlassian", "Autodesk", "Kaspersky"],
    "materialy": ["HP", "Lyreco", "Logitech", "Dell", "Jabra", "3M"],
    "meble": ["Flexispot", "Herman Miller", "Kinnarps", "Ikea Business"],
    "inne_load": ["Garant", "Drager", "Hase", "Generic Supply"],
}

DEPARTMENTS = ["IT", "Finanse", "Operacje", "HR", "Sprzedaż", "Logistyka", "Administracja", "R&D"]

TYPE_WEIGHTS = {
    "it_sprzet": 30, "wyposazenie": 25, "narzedzia": 10,
    "licencje": 10, "materialy": 12, "meble": 8, "inne_load": 5,
}

ALERT_REASONS = [
    "Koniec okresu gwarancyjnego – wymaga oceny stanu",
    "Planowany przegląd techniczny urządzenia",
    "Wymiana baterii UPS – wymagana wymiana co 3 lata",
    "Odnowienie licencji – kontakt z dostawcą",
    "Kontrola wyposażenia BHP – przegląd roczny",
    "Przegląd gaśnicy – wymóg prawny co 12 miesięcy",
    "Serwis drukarki – czyszczenie głowic i kalibracja",
    "Aktualizacja firmware urządzenia sieciowego",
    "Kalibracja multimetru – wymagana certyfikacja",
    "Weryfikacja stanu technicznego po zgłoszeniu użytkownika",
    "Przegląd instalacji elektrycznej w pomieszczeniu",
    "Planowana wymiana dysku twardego – koniec żywotności",
]


class Command(BaseCommand):
    help = "Generuje realistyczne dane load-demo dla Lupine AMS (v1). Zakres: lokalizacje, użytkownicy, assety, alerty, sesje inventory."

    def add_arguments(self, parser):
        parser.add_argument("--assets", type=int, default=5000, help="Liczba assetów (domyślnie 5000)")
        parser.add_argument("--locations", type=int, default=200, help="Docelowa liczba lokalizacji (domyślnie 200)")
        parser.add_argument("--users", type=int, default=40, help="Łączna liczba użytkowników (domyślnie 40)")
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Ziarno losowości (domyślnie 42000)")
        parser.add_argument("--batch-size", type=int, default=500, help="Rozmiar batch dla bulk_create (domyślnie 500)")

    def handle(self, *args, **options):
        existing = Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).count()
        if existing:
            raise CommandError(
                f"Znaleziono {existing} assetów load-demo w bazie. "
                "Uruchom najpierw: python manage.py clear_lupine_load_demo"
            )

        asset_count = max(options["assets"], 10)
        location_count = max(options["locations"], 10)
        user_count = max(options["users"], 4)
        batch_size = options["batch_size"]
        rng = Random(options["seed"])

        self.stdout.write(f"[load-demo] Start seeda: {asset_count} assetów, ~{location_count} lokalizacji, {user_count} użytkowników")

        with transaction.atomic():
            asset_types = self._seed_asset_types()
            self.stdout.write(f"[load-demo]   AssetTypes: {len(asset_types)}")

            locations, leaf_locations, city_roots = self._seed_locations(location_count)
            self.stdout.write(f"[load-demo]   Lokalizacje: {len(locations)} (liście: {len(leaf_locations)})")

            users, admin_user = self._seed_users(user_count, city_roots, rng)
            self.stdout.write(f"[load-demo]   Użytkownicy: {len(users)}")

            assets = self._seed_assets(asset_count, asset_types, leaf_locations, users, rng, batch_size)
            self.stdout.write(f"[load-demo]   Assety: {len(assets)}")

            alert_count = self._seed_service_alerts(assets, admin_user, rng, batch_size)
            self.stdout.write(f"[load-demo]   Alerty serwisowe: {alert_count}")

        # Sesje inventory poza główną transakcją – start_inventory_session jest @transaction.atomic
        sessions = self._seed_inventory_sessions(admin_user, city_roots, asset_types, rng)
        self.stdout.write(f"[load-demo]   Sesje inventory: {len(sessions)}")

        self.stdout.write(self.style.SUCCESS("[load-demo] Seed zakończony pomyślnie."))
        self.stdout.write(f"  Hasło użytkowników demo: load-demo-2026!")

    # ── Asset types (idempotent) ──────────────────────────────────────────────

    def _seed_asset_types(self):
        result = {}
        for code, name, is_qty, sort_order, prefix in ASSET_TYPES_SPEC:
            obj, _ = AssetTypeDictionary.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "barcode_prefix": prefix,
                    "is_quantity_based": is_qty,
                    "is_active": True,
                    "sort_order": sort_order,
                    "is_system": True,
                },
            )
            result[code] = obj
        return result

    # ── Locations ────────────────────────────────────────────────────────────

    def _seed_locations(self, target_count):
        def upsert(name, parent=None):
            obj, _ = Location.objects.update_or_create(
                name=name, parent=parent, defaults={"is_active": True}
            )
            return obj

        root = upsert(LOAD_ROOT_NAME)
        all_locations = {root.path: root}
        leaf_locations = []
        city_roots = {}
        remaining = target_count - 1

        for city_name in CITY_SITES:
            if remaining <= 0:
                break
            city = upsert(city_name, root)
            all_locations[city.path] = city
            city_roots[city_name] = city
            remaining -= 1

            for site_name in CITY_SITES[city_name]:
                if remaining <= 0:
                    break
                site = upsert(site_name, city)
                all_locations[site.path] = site
                remaining -= 1

                for floor_name in FLOORS:
                    if remaining <= 0:
                        break
                    floor = upsert(floor_name, site)
                    all_locations[floor.path] = floor
                    remaining -= 1

                    for room_name in ROOMS:
                        if remaining <= 0:
                            break
                        room = upsert(room_name, floor)
                        all_locations[room.path] = room
                        leaf_locations.append(room)
                        remaining -= 1

        if not leaf_locations:
            leaf_locations = list(all_locations.values())[-5:]

        return all_locations, leaf_locations, city_roots

    # ── Users ────────────────────────────────────────────────────────────────

    def _seed_users(self, total_count, city_roots, rng):
        User = get_user_model()
        users = []
        city_list = list(city_roots.values())

        for i in range(1, 3):
            u = self._upsert_user(
                User,
                username=f"{LOAD_USER_PREFIX}admin_{i:02d}",
                email=f"load.admin{i}@lupine.demo",
                is_staff=True,
                is_superuser=True,
                role=UserProfile.Role.ADMIN,
                can_approve=True,
                requires_approval=False,
                allowed_roots=[],
            )
            users.append(u)
        admin_user = users[0]

        manager_count = max(3, total_count // 10)
        for i in range(1, manager_count + 1):
            roots = rng.sample(city_list, min(2, len(city_list))) if city_list else []
            u = self._upsert_user(
                User,
                username=f"{LOAD_USER_PREFIX}mgr_{i:02d}",
                email=f"load.manager{i}@lupine.demo",
                is_staff=False,
                is_superuser=False,
                role=UserProfile.Role.MANAGER,
                can_approve=True,
                requires_approval=False,
                allowed_roots=roots,
            )
            users.append(u)

        regular_count = max(1, total_count - 2 - manager_count)
        for i in range(1, regular_count + 1):
            roots = rng.sample(city_list, 1) if city_list else []
            u = self._upsert_user(
                User,
                username=f"{LOAD_USER_PREFIX}user_{i:03d}",
                email=f"load.user{i}@lupine.demo",
                is_staff=False,
                is_superuser=False,
                role=UserProfile.Role.USER,
                can_approve=False,
                requires_approval=rng.random() < 0.3,
                allowed_roots=roots,
            )
            users.append(u)

        return users, admin_user

    def _upsert_user(self, User, *, username, email, is_staff, is_superuser, role, can_approve, requires_approval, allowed_roots):
        user, _ = User.objects.update_or_create(
            username=username,
            defaults={"email": email, "is_staff": is_staff, "is_superuser": is_superuser, "is_active": True},
        )
        user.set_password("load-demo-2026!")
        user.save(update_fields=["password", "email", "is_staff", "is_superuser", "is_active"])
        profile = user.profile
        profile.role = role
        profile.can_approve_asset_changes = can_approve
        profile.asset_changes_require_approval = requires_approval
        profile.save(update_fields=["role", "can_approve_asset_changes", "asset_changes_require_approval"])
        profile.allowed_locations.set(allowed_roots)
        return user

    # ── Assets ───────────────────────────────────────────────────────────────

    def _seed_assets(self, count, asset_types, leaf_locations, users, rng, batch_size):
        Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).delete()

        type_codes = list(TYPE_WEIGHTS.keys())
        type_weights = [TYPE_WEIGHTS[c] for c in type_codes]
        statuses = [Asset.Status.ACTIVE, Asset.Status.INACTIVE, Asset.Status.LIQUIDATED]
        status_weights = [60, 25, 15]
        conditions = [
            Asset.TechnicalCondition.NEW, Asset.TechnicalCondition.VERY_GOOD,
            Asset.TechnicalCondition.GOOD, Asset.TechnicalCondition.AVERAGE,
            Asset.TechnicalCondition.POOR, Asset.TechnicalCondition.DAMAGED,
        ]
        condition_weights = [10, 25, 40, 18, 5, 2]

        today = timezone.now().date()
        active_users = [u for u in users if not u.is_superuser]
        n_locs = len(leaf_locations)
        n_users = len(active_users)

        to_create = []
        for index in range(1, count + 1):
            type_code = rng.choices(type_codes, weights=type_weights, k=1)[0]
            asset_type = asset_types[type_code]
            location = leaf_locations[(index * 31 + rng.randrange(n_locs)) % n_locs]
            status = rng.choices(statuses, weights=status_weights, k=1)[0]
            condition = rng.choices(conditions, weights=condition_weights, k=1)[0]
            is_liquidated = status == Asset.Status.LIQUIDATED
            is_quantity = asset_type.is_quantity_based

            if is_quantity:
                qty = rng.choice([5, 10, 15, 20, 25, 30, 50, 75, 100])
            elif type_code in {"wyposazenie", "inne_load"} and index % 9 == 0:
                qty = rng.choice([2, 3, 4, 5])
            else:
                qty = 1

            name_pool = ASSET_NAMES[type_code]
            name = f"{name_pool[index % len(name_pool)]} #{index:05d}"
            mfr_pool = MANUFACTURERS[type_code]

            purchase_days_ago = rng.randint(30, 1825)
            purchase_date = today - timezone.timedelta(days=purchase_days_ago)
            commission_date = purchase_date + timezone.timedelta(days=rng.randint(0, 45))
            warranty_until = purchase_date + timezone.timedelta(days=rng.choice([365, 730, 1095, 1825]))

            ancestors = location.get_ancestors(include_self=True)
            org_unit = ancestors[1] if len(ancestors) > 1 else ancestors[0]

            responsible = active_users[index % n_users] if n_users else None

            to_create.append(Asset(
                name=name,
                inventory_number=f"LD-{index:06d}",
                asset_type=asset_type.code,          # must set manually – bulk_create skips save()
                asset_type_ref=asset_type,
                record_quantity=qty,
                current_quantity=0 if is_liquidated and index % 2 == 0 else qty,
                category=self._category(type_code),
                manufacturer=mfr_pool[index % len(mfr_pool)],
                model=f"Model {100 + index % 900}",
                serial_number=f"SN-LD-{index:07d}",
                barcode=f"BC-LOAD-{index:06d}",
                description=f"Asset load-demo #{index:05d} – {asset_type.name}.",
                purchase_date=purchase_date,
                commissioning_date=commission_date,
                purchase_value=Decimal(str(round(200 + (index * 13) % 49800, 2))),
                invoice_number=f"FV/LOAD/{index:06d}",
                cost_center=f"CC-{100 + index % 50:03d}",
                organizational_unit=org_unit,
                department=DEPARTMENTS[index % len(DEPARTMENTS)],
                location=location.path,              # denormalized cache – must set manually
                location_fk=location,
                room=location.name,
                responsible_person=responsible,
                status=status,
                technical_condition=condition,
                is_active=not is_liquidated,         # must sync manually – bulk_create skips save()
                warranty_until=warranty_until,
                next_review_date=today + timezone.timedelta(days=rng.randint(-180, 365)),
                external_id=f"{LOAD_DEMO_PREFIX}{index}",
            ))

        Asset.objects.bulk_create(to_create, batch_size=batch_size)
        return list(Asset.objects.filter(external_id__startswith=LOAD_DEMO_PREFIX).order_by("id"))

    def _category(self, type_code):
        return {
            "it_sprzet": "IT i infrastruktura",
            "wyposazenie": "Wyposażenie operacyjne",
            "narzedzia": "Narzędzia i serwis",
            "licencje": "Licencje i oprogramowanie",
            "materialy": "Materiały eksploatacyjne",
            "meble": "Meble i wyposażenie biurowe",
            "inne_load": "Pozostałe",
        }[type_code]

    # ── Service Alerts ───────────────────────────────────────────────────────

    def _seed_service_alerts(self, assets, admin_user, rng, batch_size):
        today = timezone.now().date()
        active_assets = [a for a in assets if a.status == Asset.Status.ACTIVE]
        alert_pool_size = max(1, len(active_assets) * 8 // 100)
        alert_assets = rng.sample(active_assets, min(len(active_assets), alert_pool_size))

        alerts = []
        for asset in alert_assets:
            n = rng.choices([1, 2, 3], weights=[70, 22, 8], k=1)[0]
            for _ in range(n):
                date_mode = rng.randint(0, 3)
                if date_mode == 0:
                    alert_date = today - timezone.timedelta(days=rng.randint(1, 90))
                elif date_mode == 1:
                    alert_date = today + timezone.timedelta(days=rng.randint(0, 7))
                elif date_mode == 2:
                    alert_date = today + timezone.timedelta(days=rng.randint(8, 30))
                else:
                    alert_date = today + timezone.timedelta(days=rng.randint(31, 180))

                roll = rng.random()
                if roll < 0.55:
                    status = AssetServiceAlert.Status.ACTIVE
                    resolved_by = None
                    resolved_at = None
                elif roll < 0.85:
                    status = AssetServiceAlert.Status.DONE
                    resolved_by = admin_user
                    resolved_at = timezone.now() - timezone.timedelta(days=rng.randint(1, 60))
                else:
                    status = AssetServiceAlert.Status.CANCELLED
                    resolved_by = admin_user
                    resolved_at = timezone.now() - timezone.timedelta(days=rng.randint(1, 30))

                alerts.append(AssetServiceAlert(
                    asset=asset,
                    reason=rng.choice(ALERT_REASONS),
                    alert_date=alert_date,
                    status=status,
                    created_by=admin_user,
                    resolved_by=resolved_by,
                    resolved_at=resolved_at,
                ))

        AssetServiceAlert.objects.bulk_create(alerts, batch_size=batch_size)
        return len(alerts)

    # ── Inventory Sessions ───────────────────────────────────────────────────

    def _seed_inventory_sessions(self, admin_user, city_roots, asset_types, rng):
        InventorySession.objects.filter(number__startswith=LOAD_SESSION_PREFIX).delete()
        all_type_codes = list(asset_types.keys())
        city_names = list(city_roots.keys())
        if not city_names:
            return []

        configs = [
            {"roots": city_names[:2],            "types": all_type_codes,                          "close": False},
            {"roots": city_names[2:4],            "types": ["it_sprzet", "wyposazenie", "licencje"], "close": False},
            {"roots": city_names[4:],             "types": all_type_codes,                          "close": False},
            {"roots": city_names[:3],             "types": ["it_sprzet", "materialy"],              "close": True},
            {"roots": city_names[1:4],            "types": all_type_codes,                          "close": True},
        ]

        sessions = []
        for offset, cfg in enumerate(configs):
            root_locs = [city_roots[n] for n in cfg["roots"] if n in city_roots]
            if not root_locs:
                continue
            # Fallback if types not present
            available_types = [t for t in cfg["types"] if t in asset_types]
            if not available_types:
                available_types = all_type_codes

            session = start_inventory_session(
                created_by=admin_user,
                root_locations=root_locs,
                asset_types=available_types,
            )
            session.number = f"{LOAD_SESSION_PREFIX}{len(sessions) + 1:03d}"
            session.started_at = timezone.now() - timezone.timedelta(days=12 - offset * 2)
            session.save(update_fields=["number", "started_at", "updated_at"])

            self._seed_observed_items(session, rng)

            if cfg["close"]:
                session.status = InventorySession.Status.CLOSED
                session.closed_at = timezone.now() - timezone.timedelta(days=max(1, 8 - offset))
                session.save(update_fields=["status", "closed_at", "updated_at"])

            sessions.append(session)

        return sessions

    def _seed_observed_items(self, session, rng):
        snapshot_items = list(
            session.snapshot_items.select_related("asset").order_by("id")[:600]
        )
        if not snapshot_items:
            return

        now = timezone.now()
        wrong_location = (
            session.scope_root_locations.exclude(
                id__in={s.location_fk_id_snapshot for s in snapshot_items}
            ).order_by("id").first()
            or session.scope_root_locations.order_by("id").first()
        )

        observed = []
        manual_qtys = []
        manual_confs = []
        seen_asset_ids = set()

        for i, item in enumerate(snapshot_items):
            asset = item.asset
            if asset is None or asset.id in seen_asset_ids:
                continue
            seen_asset_ids.add(asset.id)

            mode = i % 10
            is_quantity = item.asset_type == "quantity"

            if is_quantity:
                expected = item.record_quantity_snapshot or 1
                actual = max(0, expected + rng.randint(-2, 3))
                loc_id = (
                    wrong_location.id if mode >= 7 and wrong_location
                    else item.location_fk_id_snapshot
                )
                status = (
                    InventoryObservedItem.Status.FOUND_OTHER_LOCATION if mode >= 7
                    else InventoryObservedItem.Status.FOUND_OK
                )
                observed.append(InventoryObservedItem(
                    session=session, asset=asset,
                    code=asset.barcode or asset.inventory_number,
                    scanned_location_id=loc_id, status=status,
                    first_seen_at=now, last_seen_at=now,
                ))
                manual_qtys.append(InventorySessionManualQuantity(
                    session=session, asset=asset, quantity=actual, updated_by=None,
                ))
            else:
                if mode < 5:
                    observed.append(InventoryObservedItem(
                        session=session, asset=asset,
                        code=asset.barcode or asset.inventory_number,
                        scanned_location_id=item.location_fk_id_snapshot,
                        status=InventoryObservedItem.Status.FOUND_OK,
                        first_seen_at=now, last_seen_at=now,
                    ))
                elif mode < 7:
                    loc_id = wrong_location.id if wrong_location else item.location_fk_id_snapshot
                    observed.append(InventoryObservedItem(
                        session=session, asset=asset,
                        code=asset.barcode or asset.inventory_number,
                        scanned_location_id=loc_id,
                        status=InventoryObservedItem.Status.FOUND_OTHER_LOCATION,
                        first_seen_at=now, last_seen_at=now,
                    ))
                elif mode == 7:
                    manual_confs.append(
                        InventorySessionManualConfirmation(session=session, asset=asset, confirmed_by=None)
                    )

        for j in range(1, 6):
            observed.append(InventoryObservedItem(
                session=session, asset=None,
                code=f"UNKNOWN-{session.number}-{j:03d}",
                scanned_location=None,
                status=InventoryObservedItem.Status.UNKNOWN_CODE,
                first_seen_at=now, last_seen_at=now,
            ))

        InventoryObservedItem.objects.bulk_create(observed, ignore_conflicts=True, batch_size=500)
        InventorySessionManualQuantity.objects.bulk_create(manual_qtys, ignore_conflicts=True, batch_size=200)
        InventorySessionManualConfirmation.objects.bulk_create(manual_confs, ignore_conflicts=True, batch_size=200)

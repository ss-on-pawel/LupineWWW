from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from locations.models import Location


DEFAULT_CITY_NAMES = [
    "Warszawa",
    "Gdańsk",
    "Kraków",
    "Wrocław",
    "Poznań",
    "Łódź",
    "Katowice",
]

STANDARD_BRANCH_TYPES = [
    "office",
    "warehouse",
    "service",
    "lab",
    "regional",
    "production",
]
LARGE_CITY_BRANCH_TYPES = [
    "office_compact",
    "regional_compact",
    "warehouse_compact",
    "service_compact",
]
LARGE_CITY_THRESHOLD = 30

OFFICE_NAMES = [
    "Budynek A",
    "Budynek B",
    "Budynek C",
    "Budynek D",
]
WAREHOUSE_NAMES = [
    "Magazyn Główny",
    "Magazyn Wysoki",
    "Centrum Dystrybucji",
    "Magazyn Operacyjny",
]
SERVICE_NAMES = [
    "Centrum Serwisowe",
    "Serwis Techniczny",
    "Warsztat Główny",
]
LAB_NAMES = [
    "Laboratorium",
    "Laboratorium Badawcze",
    "Laboratorium Kalibracji",
]
REGIONAL_NAMES = [
    "Biuro Regionalne",
    "Oddział Handlowy",
    "Centrum Obsługi Klienta",
]
PRODUCTION_NAMES = [
    "Zakład Produkcyjny",
    "Hala Operacyjna",
    "Obiekt Techniczny",
]


class Command(BaseCommand):
    help = "Generuje realistyczną, hierarchiczną strukturę testowych lokalizacji do developmentu."

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=1000,
            help="Docelowa liczba lokalizacji do utworzenia.",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Usuwa istniejące lokalizacje przed seedowaniem.",
        )
        parser.add_argument(
            "--cities",
            type=int,
            default=len(DEFAULT_CITY_NAMES),
            help="Liczba głównych gałęzi miejskich do wygenerowania.",
        )

    def handle(self, *args, **options):
        target_count = max(options["count"], 0)
        city_count = options["cities"]
        clear = options["clear"]

        if city_count < 1:
            raise CommandError("--cities musi być większe lub równe 1.")

        if target_count < city_count:
            raise CommandError("--count nie może być mniejsze niż liczba korzeni miast.")

        with transaction.atomic():
            existing_count = Location.objects.count()
            if existing_count and not clear:
                raise CommandError(
                    "Tabela Location nie jest pusta. Użyj pustej bazy albo uruchom komendę z flagą --clear."
                )

            deleted_count = 0
            if clear and existing_count:
                deleted_count = self._clear_locations()

            created_count, root_names = self._seed_locations(
                target_count=target_count,
                city_names=self._build_city_names(city_count),
            )

        if clear:
            self.stdout.write(
                self.style.WARNING(f"Usunięto {deleted_count} istniejących lokalizacji przed seedem.")
            )

        average_per_city = created_count / city_count if city_count else 0
        sample_roots = ", ".join(root_names[: min(5, len(root_names))])
        self.stdout.write(self.style.SUCCESS("Seed lokalizacji zakończony."))
        self.stdout.write(f"Liczba miast: {city_count}")
        self.stdout.write(f"Liczba wszystkich lokalizacji: {created_count}")
        self.stdout.write(f"Średnia liczba lokalizacji na miasto: {average_per_city:.2f}")
        self.stdout.write(f"Użyto --clear: {'tak' if clear else 'nie'}")
        self.stdout.write(f"Top-level roots: {sample_roots}")

    def _build_city_names(self, city_count):
        if city_count <= len(DEFAULT_CITY_NAMES):
            return DEFAULT_CITY_NAMES[:city_count]
        return [f"Miasto {index:03d}" for index in range(1, city_count + 1)]

    def _seed_locations(self, *, target_count, city_names):
        if target_count == 0:
            return 0, []

        city_quotas = self._distribute_counts(target_count, len(city_names))
        use_large_city_layout = len(city_names) > LARGE_CITY_THRESHOLD
        created_count = 0
        root_names = []

        for city_index, city_name in enumerate(city_names, start=1):
            city = Location.objects.create(name=city_name, parent=None)
            root_names.append(city.name)
            created_count += 1

            city_budget = city_quotas[city_index - 1] - 1
            if city_budget <= 0:
                continue

            created_count += self._seed_city_children(
                city=city,
                budget=city_budget,
                city_index=city_index,
                use_large_city_layout=use_large_city_layout,
            )

        return created_count, root_names

    def _distribute_counts(self, total_count, bucket_count):
        base = total_count // bucket_count
        extra = total_count % bucket_count
        return [
            base + (1 if bucket_index < extra else 0)
            for bucket_index in range(bucket_count)
        ]

    def _seed_city_children(self, *, city, budget, city_index, use_large_city_layout):
        created_count = 0
        branch_types = LARGE_CITY_BRANCH_TYPES if use_large_city_layout else STANDARD_BRANCH_TYPES
        branch_counters = {branch_type: 0 for branch_type in branch_types}
        branch_index = 0

        while budget > 0:
            branch_type = branch_types[branch_index % len(branch_types)]
            branch_counters[branch_type] += 1
            branch_index += 1

            subtree = self._build_branch(
                branch_type=branch_type,
                branch_index=branch_counters[branch_type],
                city_index=city_index,
            )
            trimmed_subtree = self._trim_subtree(subtree, budget)
            created_now = self._create_subtree(trimmed_subtree, parent=city)
            created_count += created_now
            budget -= created_now

        return created_count

    def _build_branch(self, *, branch_type, branch_index, city_index):
        builders = {
            "office": self._build_office_branch,
            "warehouse": self._build_warehouse_branch,
            "service": self._build_service_branch,
            "lab": self._build_lab_branch,
            "regional": self._build_regional_branch,
            "production": self._build_production_branch,
            "office_compact": self._build_office_compact_branch,
            "regional_compact": self._build_regional_compact_branch,
            "warehouse_compact": self._build_warehouse_compact_branch,
            "service_compact": self._build_service_compact_branch,
        }
        return builders[branch_type](branch_index=branch_index, city_index=city_index)

    def _build_office_branch(self, *, branch_index, city_index):
        floor_count = 3 if branch_index % 2 else 2
        children = [{"name": "Recepcja"}, {"name": "Sekretariat"}]
        for floor_number in range(1, floor_count + 1):
            floor_children = [
                {"name": f"Pokój {floor_number}01"},
                {"name": f"Pokój {floor_number}02"},
                {"name": f"Pokój {floor_number}03"},
                {"name": f"Pokój {floor_number}04"},
            ]
            if floor_number == 1:
                floor_children.append({"name": "Sala Konferencyjna"})
            if floor_number == floor_count:
                floor_children.append({"name": "Serwerownia"})
            if floor_number == 2:
                floor_children.append({"name": "Archiwum"})
            children.append({"name": f"Piętro {floor_number}", "children": floor_children})
        return {"name": self._series_name(OFFICE_NAMES, branch_index), "children": children}

    def _build_warehouse_branch(self, *, branch_index, city_index):
        zone_count = 3 if branch_index % 2 else 4
        children = [
            {"name": "Przyjęcia"},
            {"name": "Wydania"},
            {"name": "Biuro Magazynu"},
        ]
        for zone_index in range(zone_count):
            zone_name = f"Strefa {chr(ord('A') + zone_index)}"
            zone_children = [
                {"name": "Aleja 1"},
                {"name": "Aleja 2"},
            ]
            if zone_index == 0:
                zone_children.append({"name": "Strefa Zwrotów"})
            children.append({"name": zone_name, "children": zone_children})
        return {"name": self._series_name(WAREHOUSE_NAMES, branch_index), "children": children}

    def _build_service_branch(self, *, branch_index, city_index):
        children = [
            {"name": "Recepcja"},
            {
                "name": "Warsztat Mechaniczny",
                "children": [
                    {"name": "Stanowisko 1"},
                    {"name": "Stanowisko 2"},
                    {"name": "Stanowisko 3"},
                ],
            },
            {
                "name": "Warsztat Elektroniki",
                "children": [
                    {"name": "Stanowisko 1"},
                    {"name": "Stanowisko 2"},
                ],
            },
            {
                "name": "Magazyn Części",
                "children": [{"name": "Strefa A"}, {"name": "Strefa B"}],
            },
            {"name": "Serwerownia"},
        ]
        return {"name": self._series_name(SERVICE_NAMES, branch_index), "children": children}

    def _build_lab_branch(self, *, branch_index, city_index):
        room_count = 3 if branch_index % 2 else 2
        children = [{"name": "Recepcja"}]
        for room_number in range(1, room_count + 1):
            children.append(
                {
                    "name": f"Pracownia {room_number}",
                    "children": [{"name": "Sekcja Przygotowania"}, {"name": "Magazyn Próbek"}],
                }
            )
        children.extend([{"name": "Chłodnia"}, {"name": "Serwerownia"}])
        return {"name": self._series_name(LAB_NAMES, branch_index), "children": children}

    def _build_regional_branch(self, *, branch_index, city_index):
        children = [
            {"name": "Recepcja"},
            {
                "name": "Dział Sprzedaży",
                "children": [{"name": "Pokój 1"}, {"name": "Pokój 2"}, {"name": "Sala Spotkań"}],
            },
            {
                "name": "Dział Administracji",
                "children": [{"name": "Sekretariat"}, {"name": "Archiwum"}],
            },
            {"name": "Sala Szkoleniowa"},
        ]
        if branch_index % 2:
            children.append({"name": "Open Space"})
        return {"name": self._series_name(REGIONAL_NAMES, branch_index), "children": children}

    def _build_production_branch(self, *, branch_index, city_index):
        line_count = 2 if branch_index % 2 else 3
        children = [
            {
                "name": "Hala 1",
                "children": [{"name": "Linia 1"}, {"name": "Linia 2"}],
            },
            {
                "name": "Hala 2",
                "children": [{"name": f"Linia {line_number}"} for line_number in range(1, line_count + 1)],
            },
            {"name": "Kontrola Jakości"},
            {"name": "Magazyn Surowców"},
            {"name": "Serwerownia"},
        ]
        return {"name": self._series_name(PRODUCTION_NAMES, branch_index), "children": children}

    def _build_office_compact_branch(self, *, branch_index, city_index):
        suffix = (city_index + branch_index) % 3
        children = [{"name": "Recepcja"}]
        if suffix != 0:
            children.append({"name": "Pokój 101"})
        if suffix == 2:
            children.append({"name": "Pokój 102"})
        return {"name": self._series_name(["Budynek A", "Budynek B", "Budynek C"], branch_index), "children": children}

    def _build_regional_compact_branch(self, *, branch_index, city_index):
        children = [{"name": "Recepcja"}, {"name": "Sekcja A"}]
        if (city_index + branch_index) % 2 == 0:
            children.append({"name": "Sekcja B"})
        return {"name": self._series_name(["Oddział 1", "Oddział 2", "Oddział 3"], branch_index), "children": children}

    def _build_warehouse_compact_branch(self, *, branch_index, city_index):
        children = [{"name": "Strefa A"}]
        if branch_index % 2:
            children.append({"name": "Strefa B"})
        children.append({"name": "Biuro Magazynu"})
        return {"name": self._series_name(["Magazyn 1", "Magazyn 2", "Magazyn 3"], branch_index), "children": children}

    def _build_service_compact_branch(self, *, branch_index, city_index):
        children = [{"name": "Serwis"}, {"name": "Sekcja A"}]
        if (city_index + branch_index) % 2:
            children.append({"name": "Pokój 201"})
        return {"name": self._series_name(["Punkt Techniczny", "Zaplecze", "Obiekt Pomocniczy"], branch_index), "children": children}

    def _series_name(self, base_names, index):
        base_name = base_names[(index - 1) % len(base_names)]
        cycle_number = (index - 1) // len(base_names) + 1
        if cycle_number == 1:
            return base_name
        return f"{base_name} {cycle_number}"

    def _trim_subtree(self, node, limit):
        trimmed = {"name": node["name"]}
        if limit <= 1 or not node.get("children"):
            return trimmed

        children = []
        remaining = limit - 1
        for child in node["children"]:
            if remaining <= 0:
                break
            child_size = self._count_nodes(child)
            child_limit = min(child_size, remaining)
            trimmed_child = self._trim_subtree(child, child_limit)
            children.append(trimmed_child)
            remaining -= self._count_nodes(trimmed_child)

        if children:
            trimmed["children"] = children
        return trimmed

    def _count_nodes(self, node):
        return 1 + sum(self._count_nodes(child) for child in node.get("children", []))

    def _create_subtree(self, node, *, parent):
        location = Location.objects.create(name=node["name"], parent=parent)
        created_count = 1
        for child in node.get("children", []):
            created_count += self._create_subtree(child, parent=location)
        return created_count

    def _clear_locations(self):
        deleted_count = 0
        while Location.objects.exists():
            leaf_ids = list(Location.objects.filter(children__isnull=True).values_list("id", flat=True))
            if not leaf_ids:
                break
            batch_count, _ = Location.objects.filter(id__in=leaf_ids).delete()
            deleted_count += batch_count
        return deleted_count

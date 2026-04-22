from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from locations.models import Location


LOCATION_TREE = [
    {
        "name": "Warszawa",
        "children": [
            {
                "name": "Centrala",
                "children": [
                    {"name": "Sekretariat"},
                    {"name": "Pokój 1"},
                    {"name": "Pokój 2"},
                    {"name": "Pokój 3"},
                    {"name": "Sala Zarządu"},
                    {"name": "Serwerownia"},
                    {"name": "Archiwum"},
                ],
            },
            {
                "name": "Magazyn A",
                "children": [
                    {"name": "Strefa 1"},
                    {"name": "Strefa 2"},
                    {"name": "Strefa 3"},
                    {"name": "Przyjęcia"},
                    {"name": "Wydania"},
                ],
            },
            {
                "name": "Budynek B",
                "children": [
                    {"name": "Recepcja"},
                    {"name": "Pokój 1"},
                    {"name": "Pokój 4"},
                    {"name": "Serwerownia"},
                ],
            },
        ],
    },
    {
        "name": "Kraków",
        "children": [
            {
                "name": "Biuro",
                "children": [
                    {"name": "Sekretariat"},
                    {"name": "Pokój 1"},
                    {"name": "Pokój 2"},
                    {"name": "Sala Spotkań"},
                ],
            },
            {
                "name": "Magazyn",
                "children": [
                    {"name": "Strefa 1"},
                    {"name": "Strefa 2"},
                    {"name": "Zaplecze"},
                ],
            },
            {
                "name": "Laboratorium",
                "children": [
                    {"name": "Pracownia 1"},
                    {"name": "Pracownia 2"},
                    {"name": "Serwerownia"},
                ],
            },
        ],
    },
    {
        "name": "Gdańsk",
        "children": [
            {
                "name": "Oddział",
                "children": [
                    {"name": "Recepcja"},
                    {"name": "Pokój 1"},
                    {"name": "Pokój 2"},
                    {"name": "Sekretariat"},
                ],
            },
            {
                "name": "Magazyn",
                "children": [
                    {"name": "Strefa 1"},
                    {"name": "Strefa 2"},
                    {"name": "Magazyn"},
                ],
            },
            {
                "name": "Zaplecze Techniczne",
                "children": [
                    {"name": "Serwerownia"},
                    {"name": "Warsztat"},
                ],
            },
        ],
    },
    {
        "name": "Poznań",
        "children": [
            {
                "name": "Biuro Regionalne",
                "children": [
                    {"name": "Sekretariat"},
                    {"name": "Pokój 1"},
                    {"name": "Pokój 2"},
                    {"name": "Sala Szkoleniowa"},
                ],
            },
            {
                "name": "Centrum Dystrybucji",
                "children": [
                    {"name": "Strefa A"},
                    {"name": "Strefa B"},
                    {"name": "Strefa C"},
                    {"name": "Magazyn"},
                ],
            },
        ],
    },
    {
        "name": "Wrocław",
        "children": [
            {
                "name": "Oddział",
                "children": [
                    {"name": "Recepcja"},
                    {"name": "Pokój 1"},
                    {"name": "Pokój 2"},
                    {"name": "Pokój 3"},
                ],
            },
            {
                "name": "Serwis",
                "children": [
                    {"name": "Przyjęcia"},
                    {"name": "Warsztat"},
                    {"name": "Magazyn"},
                ],
            },
        ],
    },
    {
        "name": "Łódź",
        "children": [
            {
                "name": "Zakład",
                "children": [
                    {"name": "Hala 1"},
                    {"name": "Hala 2"},
                    {"name": "Magazyn"},
                    {"name": "Serwerownia"},
                ],
            },
            {
                "name": "Administracja",
                "children": [
                    {"name": "Sekretariat"},
                    {"name": "Pokój 1"},
                    {"name": "Archiwum"},
                ],
            },
        ],
    },
]


class Command(BaseCommand):
    help = "Tworzy hierarchiczne dane testowe lokalizacji do developmentu."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Usuń istniejące lokalizacje przed seedowaniem. Używaj wyłącznie w development.",
        )

    def handle(self, *args, **options):
        clear = options["clear"]

        with transaction.atomic():
            existing_count = Location.objects.count()

            if existing_count and not clear:
                raise CommandError(
                    "Tabela Location nie jest pusta. Uruchom seed tylko na pustych danych "
                    "albo użyj jawnie flagi --clear."
                )

            if clear and existing_count:
                deleted_count = self._clear_locations()
                self.stdout.write(self.style.WARNING(f"Usunięto {deleted_count} istniejących lokalizacji."))

            created_count = self._seed_tree()

        self.stdout.write(
            self.style.SUCCESS(
                f"Zakończono seed lokalizacji. Utworzono {created_count} rekordów hierarchicznych."
            )
        )

    def _seed_tree(self):
        created_count = 0
        for root_node in LOCATION_TREE:
            created_count += self._create_node(root_node, parent=None)
        return created_count

    def _create_node(self, node, *, parent):
        location = Location.objects.create(name=node["name"], parent=parent)
        created_count = 1
        for child in node.get("children", []):
            created_count += self._create_node(child, parent=location)
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

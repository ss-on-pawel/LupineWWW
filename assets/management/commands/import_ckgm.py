"""
Management command: import_ckgm
Importuje środki trwałe CKGM z pliku ckgm_import.json.

Użycie:
    python manage.py import_ckgm /ścieżka/do/ckgm_import.json
    python manage.py import_ckgm /ścieżka/do/ckgm_import.json --dry-run

Dry-run: pokazuje statystyki bez zapisu do bazy.
"""

import json
import sys
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from locations.models import Location
from assets.models import Asset, AssetTypeDictionary


class Command(BaseCommand):
    help = 'Import środków trwałych CKGM z ckgm_import.json'

    def add_arguments(self, parser):
        parser.add_argument('json_file', type=str, help='Ścieżka do ckgm_import.json')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Tylko raport — bez zapisu do bazy'
        )

    def handle(self, *args, **options):
        json_path = options['json_file']
        dry_run   = options['dry_run']

        self.stdout.write(f"Wczytuję: {json_path}")
        try:
            with open(json_path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"Plik nie istnieje: {json_path}")
        except json.JSONDecodeError as e:
            raise CommandError(f"Błąd JSON: {e}")

        stats = data.get('stats', {})
        self.stdout.write(
            f"JSON: {len(data['assets'])} środków "
            f"({stats.get('active',0)} aktywnych, {stats.get('archived',0)} archiwalnych)"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("=== DRY RUN — brak zapisu ==="))

        try:
            with transaction.atomic():
                results = self._run_import(data, dry_run)
                if dry_run:
                    transaction.set_rollback(True)
        except Exception as e:
            raise CommandError(f"Błąd importu: {e}")

        self._print_results(results, dry_run)

    # ── Import ────────────────────────────────────────────────────────────────

    def _run_import(self, data, dry_run):
        results = {
            'locations_created': 0,
            'asset_types_created': 0,
            'assets_created': 0,
            'assets_skipped_duplicate': 0,
            'assets_skipped_error': 0,
            'errors': [],
        }

        # 1. Drzewo lokalizacji
        self.stdout.write("Tworzę drzewo lokalizacji...")
        loc_cache = {}  # path_tuple → Location
        self._build_location_tree(
            data['location_tree'], parent=None,
            loc_cache=loc_cache, results=results, dry_run=dry_run
        )
        self.stdout.write(f"  Lokalizacje: {results['locations_created']} nowych")

        # 2. AssetTypeDictionary
        self.stdout.write("Tworzę typy środków...")
        type_cache = {}  # code → AssetTypeDictionary
        for at in data['asset_types']:
            if not dry_run:
                obj, created = AssetTypeDictionary.objects.get_or_create(
                    code=at['code'],
                    defaults={
                        'name': at['name'],
                        'is_quantity_based': at['is_quantity_based'],
                        'is_active': True,
                    }
                )
                if created:
                    results['asset_types_created'] += 1
                type_cache[at['code']] = obj
            else:
                results['asset_types_created'] += 1
        self.stdout.write(f"  Typy: {results['asset_types_created']} nowych")

        # 3. Assets
        self.stdout.write(f"Importuję {len(data['assets'])} środków...")
        for i, asset_data in enumerate(data['assets']):
            if i % 500 == 0 and i > 0:
                self.stdout.write(f"  ... {i}/{len(data['assets'])}")
            self._import_asset(asset_data, loc_cache, type_cache, results, dry_run)

        return results

    def _build_location_tree(self, node, parent, loc_cache, results, dry_run):
        name = node.get('name', '')
        if not name:
            return None

        path = (parent.pk if parent else None, name)

        if not dry_run:
            obj, created = Location.objects.get_or_create(
                name=name,
                parent=parent,
                defaults={'is_active': True}
            )
            if created:
                results['locations_created'] += 1
        else:
            obj = None
            results['locations_created'] += 1

        # Klucz cache: pełna ścieżka jako tuple nazw
        ancestors = []
        p = parent
        while p is not None:
            ancestors.append(p.name)
            p = p.parent
        ancestors.reverse()
        ancestors.append(name)
        cache_key = tuple(ancestors)
        loc_cache[cache_key] = obj

        for child_node in node.get('children', {}).values():
            self._build_location_tree(child_node, obj, loc_cache, results, dry_run)

        return obj

    def _import_asset(self, ad, loc_cache, type_cache, results, dry_run):
        barcode = ad.get('barcode', '').strip()
        name    = ad.get('name', '').strip()

        if not barcode or not name:
            results['assets_skipped_error'] += 1
            results['errors'].append(f"Brak kodu/nazwy: {ad}")
            return

        if not dry_run and Asset.objects.filter(barcode=barcode).exists():
            results['assets_skipped_duplicate'] += 1
            return

        # Lokalizacja FK
        location_fk = None
        location_text = 'L'
        loc_path = ad.get('location_path')
        if loc_path:
            cache_key = tuple(loc_path)
            location_fk = loc_cache.get(cache_key)
            if location_fk:
                location_text = ' / '.join(loc_path[1:])  # bez roota

        # Typ środka
        asset_type_ref = None
        asset_type_code = ad.get('asset_type_code', 'st')
        ASSET_TYPE_MAP = {'st': 'fixed', 'nk': 'low_value', 'wnp': 'intangible'}
        asset_type_choice = ASSET_TYPE_MAP.get(asset_type_code, 'fixed')
        if not dry_run:
            asset_type_ref = type_cache.get(asset_type_code)

        # Wartość zakupu
        pv = None
        if ad.get('purchase_value') is not None:
            try:
                pv = Decimal(str(ad['purchase_value'])).quantize(Decimal('0.01'))
            except InvalidOperation:
                pv = None

        # Status: zlikwidowane → liquidated, aktywne → active
        status = 'liquidated' if not ad['is_active'] else 'active'

        if not dry_run:
            try:
                Asset.objects.create(
                    barcode=barcode,
                    name=name,
                    inventory_number=barcode,
                    is_active=ad['is_active'],
                    status=status,
                    asset_type=asset_type_choice,
                    asset_type_ref=asset_type_ref,
                    record_quantity=ad.get('quantity', 1),
                    current_quantity=ad.get('quantity', 1),
                    purchase_value=pv,
                    purchase_date=ad.get('purchase_date') or None,
                    invoice_number=ad.get('invoice_number', '')[:120],
                    category=ad.get('category', '')[:120],
                    external_id=ad.get('external_id', '')[:120],
                    location_fk=location_fk,
                    location=location_text,
                )
                results['assets_created'] += 1
            except Exception as e:
                results['assets_skipped_error'] += 1
                results['errors'].append(f"{barcode}: {e}")
        else:
            results['assets_created'] += 1

    # ── Raport ────────────────────────────────────────────────────────────────

    def _print_results(self, results, dry_run):
        mode = "DRY RUN" if dry_run else "IMPORT"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== {mode} — WYNIKI ==="))
        self.stdout.write(f"  Lokalizacje utworzone:    {results['locations_created']}")
        self.stdout.write(f"  Typy środków utworzone:   {results['asset_types_created']}")
        self.stdout.write(f"  Środki zaimportowane:     {results['assets_created']}")
        self.stdout.write(f"  Pominięto (duplikat):     {results['assets_skipped_duplicate']}")
        self.stdout.write(f"  Pominięto (błąd):         {results['assets_skipped_error']}")

        if results['errors']:
            self.stdout.write(self.style.WARNING(f"\nPierwsze 10 błędów:"))
            for err in results['errors'][:10]:
                self.stdout.write(f"  {err}")

        if not dry_run and results['assets_created'] > 0:
            self.stdout.write(self.style.SUCCESS("\nImport zakończony pomyślnie."))

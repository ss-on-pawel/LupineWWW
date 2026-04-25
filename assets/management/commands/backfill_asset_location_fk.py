from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from assets.models import Asset
from locations.models import Location


class Command(BaseCommand):
    help = "Uzupelnia Asset.location_fk tylko dla pewnych dopasowan Asset.location == Location.path."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Liczy dopasowania bez zapisywania zmian w bazie.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        path_to_locations = defaultdict(list)
        for location in Location.objects.all():
            path_to_locations[location.path].append(location)

        assets = list(Asset.objects.filter(location_fk__isnull=True).only("id", "location"))
        matched_assets = []
        empty_count = 0
        unmatched_count = 0
        ambiguous_count = 0

        for asset in assets:
            raw_location = (asset.location or "").strip()
            if not raw_location:
                empty_count += 1
                continue

            matching_locations = path_to_locations.get(raw_location, [])
            if len(matching_locations) == 1:
                asset.location_fk_id = matching_locations[0].id
                matched_assets.append(asset)
                continue

            if len(matching_locations) > 1:
                ambiguous_count += 1
            else:
                unmatched_count += 1

        if not dry_run and matched_assets:
            with transaction.atomic():
                Asset.objects.bulk_update(matched_assets, ["location_fk"], batch_size=500)

        remaining_without_fk = len(assets) - len(matched_assets)
        mode_label = "DRY-RUN" if dry_run else "WYKONANO"

        self.stdout.write(self.style.SUCCESS(f"{mode_label}: backfill Asset.location_fk"))
        self.stdout.write(f"Assety sprawdzone (location_fk IS NULL): {len(assets)}")
        self.stdout.write(f"Pewne dopasowania: {len(matched_assets)}")
        self.stdout.write(f"Puste location: {empty_count}")
        self.stdout.write(f"Bez dopasowania: {unmatched_count}")
        self.stdout.write(f"Niejednoznaczne dopasowania: {ambiguous_count}")
        self.stdout.write(f"Pozostaje bez location_fk: {remaining_without_fk}")

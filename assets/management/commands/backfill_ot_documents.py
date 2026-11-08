import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from assets.models import Asset, AssetAttachment, AssetHistoryEntry
from assets.services import record_asset_history


class Command(BaseCommand):
    help = "Odtwórz numery OT z chronionych źródłowych JSON. Domyślnie tylko analiza."

    def add_arguments(self, parser):
        parser.add_argument("--external-id-prefix", required=True)
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        prefix = options["external_id_prefix"].strip()
        if not prefix:
            raise CommandError("Wymagany niepusty prefiks identyfikatora zewnętrznego.")
        assets = Asset.objects.filter(external_id__startswith=prefix).order_by("pk")
        if options["apply"]:
            assets = assets.select_for_update()
        proposals, errors = [], []
        unchanged = empty = 0
        for asset in assets:
            attachments = list(AssetAttachment.objects.filter(
                asset=asset, title="Oryginalna kartoteka klienta", is_protected=True,
                content_type="application/json",
            ))
            sources = {}
            for attachment in attachments:
                try:
                    with attachment.file.open("rb") as source:
                        raw = source.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ValueError("Zbyt duży plik źródłowy")
                    value = json.loads(raw)["fields"]["Dokument OT"]
                    if value is not None and value != "":
                        if not isinstance(value, str):
                            raise ValueError("Numer OT nie jest tekstem; wymaga analizy")
                        # Preserve the exact source number, including whitespace.
                        if len(value) > 120:
                            raise ValueError("Numer OT przekracza 120 znaków")
                        sources[value] = attachment
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append(f"ID {asset.pk}: {exc}")
            if len(sources) > 1:
                errors.append(f"ID {asset.pk}: sprzeczne numery OT w źródłach")
            elif not sources:
                empty += 1
            else:
                number = next(iter(sources))
                if asset.ot_document_number == number:
                    unchanged += 1
                elif asset.ot_document_number:
                    errors.append(f"ID {asset.pk}: pole OT ma już inną wartość")
                else:
                    proposals.append((asset, number, sources[number]))
        self.stdout.write(f"Do uzupełnienia: {len(proposals)}; zgodne: {unchanged}; bez OT: {empty}; błędy: {len(errors)}")
        if errors:
            for error in errors:
                self.stderr.write(error)
            raise CommandError("Nie zapisano żadnych zmian. Wyjaśnij konflikty i błędy źródeł.")
        if not options["apply"]:
            self.stdout.write("DRY RUN — bez zapisu. Zapis wymaga --apply i aktualnego backupu.")
            return
        for asset, number, attachment in proposals:
            # Avoid save() normalizing unrelated legacy status/type/location fields.
            Asset.objects.filter(pk=asset.pk).update(ot_document_number=number, updated_at=timezone.now())
            record_asset_history(
                asset=asset, event_type=AssetHistoryEntry.EventType.UPDATED,
                description="Odtworzono numer OT z oryginalnej kartoteki klienta",
                field_name="ot_document_number", old_value="", new_value=number,
                source_object=attachment,
            )
        self.stdout.write(self.style.SUCCESS(f"Uzupełniono {len(proposals)} numerów OT. Źródła zachowano."))

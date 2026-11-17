import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from assets.models import Asset, AssetAttachment, AssetHistoryEntry
from assets.services import record_asset_history


class Command(BaseCommand):
    help = 'Odtwórz katalogowanie z chronionych oryginałów JSON. Domyślnie bez zapisu.'

    def add_arguments(self, parser):
        parser.add_argument('--external-id-prefix', required=True)
        parser.add_argument('--apply', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        prefix = options['external_id_prefix'].strip()
        if not prefix:
            raise CommandError('Wymagany niepusty prefiks klienta.')
        assets = Asset.objects.filter(external_id__startswith=prefix).order_by('pk')
        if options['apply']:
            assets = assets.select_for_update()
        proposals, errors = [], []
        for asset in assets:
            attachments = list(AssetAttachment.objects.filter(asset=asset,
                title='Oryginalna kartoteka klienta', is_protected=True, content_type='application/json'))
            values = {}
            if not attachments:
                errors.append(f'ID {asset.pk}: brak chronionego źródła')
            for attachment in attachments:
                try:
                    with attachment.file.open('rb') as f:
                        raw = f.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ValueError('Zbyt duży plik')
                    value = json.loads(raw)['fields']['Katalog']
                    if value not in (None, '', 'K'):
                        raise ValueError('Nieznane oznaczenie Katalog')
                    values[value == 'K'] = attachment
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append(f'ID {asset.pk}: {exc}')
            if len(values) > 1:
                errors.append(f'ID {asset.pk}: sprzeczne źródła')
            elif True in values and not asset.is_catalogued:
                proposals.append((asset, values[True]))
        self.stdout.write(f'Do oznaczenia: {len(proposals)}; błędy: {len(errors)}')
        if errors:
            raise CommandError('\n'.join(errors) + '\nNie zapisano żadnych zmian.')
        if not options['apply']:
            self.stdout.write('DRY RUN — bez zapisu. Zapis wymaga --apply i aktualnego backupu.')
            return
        for asset, attachment in proposals:
            Asset.objects.filter(pk=asset.pk).update(is_catalogued=True, updated_at=timezone.now())
            record_asset_history(asset=asset, event_type=AssetHistoryEntry.EventType.UPDATED,
                description='Odtworzono katalogowanie z oryginalnej kartoteki klienta',
                field_name='is_catalogued', old_value='Nie', new_value='Tak', source_object=attachment)
        self.stdout.write(f'Oznaczono: {len(proposals)}. Oryginały zachowano.')

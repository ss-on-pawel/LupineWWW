import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from assets.models import AssetWithdrawal, AssetAttachment


class Command(BaseCommand):
    help = 'Odtworzenie jawnego bibliotecznego Przekazany; domyślnie tylko raport.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        candidates = []
        for attachment in AssetAttachment.objects.filter(asset__is_active=False,
                asset__external_id__startswith='biblioteka-grodzisk:',
                is_protected=True, content_type='application/json', title='Oryginalna kartoteka klienta'):
            try:
                with attachment.file.open('rb') as file:
                    data = json.load(file)
            except (OSError, ValueError) as error:
                raise CommandError(f'Nie można odczytać źródła {attachment.pk}: {error}')
            if data.get('file') != 'likwidacjaBPGGM.xlsx' or data.get('fields', {}).get('Stan') != 'Przekazany':
                continue
            if str(data['fields'].get('ID')) != attachment.asset.external_id.split(':')[-1]:
                raise CommandError(f'Niezgodność źródła {attachment.pk}')
            candidates.append(attachment)
        with transaction.atomic():
            for attachment in candidates:
                events = AssetWithdrawal.objects.select_for_update().filter(asset=attachment.asset,
                    is_historical=True, source_reference='migration:0035', method='unknown')
                if options['apply']:
                    events.update(method='transfer', source_reference=f'AssetAttachment:{attachment.pk}',
                        notes='Źródłowy stan: Przekazany. Zachowany oryginalny opis i JSON; data oraz ilość nieustalone.')
        self.stdout.write(f'{"APPLY" if options["apply"] else "DRY RUN"}: {len(candidates)} jawnych źródeł Przekazany; dat i ilości nie uzupełniano.')

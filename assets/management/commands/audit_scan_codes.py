import json
from django.core.management.base import BaseCommand
from assets.models import ScanCode


class Command(BaseCommand):
    help = 'Odczytowy raport historycznych kolizji kodów środka i lokalizacji.'

    def handle(self, *args, **options):
        rows = list(ScanCode.objects.filter(asset_id__isnull=False, location_id__isnull=False).values('code', 'asset_id', 'location_id'))
        self.stdout.write(json.dumps({'collisions': rows, 'count': len(rows)}, ensure_ascii=False))

import re
from django.db import migrations


def seed_sequences(apps, schema_editor):
    Attachment = apps.get_model('assets', 'AssetAttachment')
    Counter = apps.get_model('assets', 'LtDocumentSequence')
    alias = schema_editor.connection.alias
    maxima = {}
    for number, filename in Attachment.objects.using(alias).filter(document_type__in=['lt', 'LT']).values_list(
            'document_number', 'original_filename').iterator():
        match = re.fullmatch(r'LT/(\d+)/(\d{4})', number or '', re.I)
        if not match:
            match = re.fullmatch(r'LT_\d{8}_LT-(\d+)-(\d{4})\.pdf', filename or '', re.I)
        if match:
            sequence, year = map(int, match.groups())
            maxima[year] = max(maxima.get(year, 0), sequence)
    for year, maximum in maxima.items():
        counter, _ = Counter.objects.using(alias).get_or_create(year=year)
        if counter.next_number <= maximum:
            counter.next_number = maximum + 1
            counter.save(using=alias, update_fields=['next_number'])


class Migration(migrations.Migration):
    dependencies = [('assets', '0038_ltdocumentsequence_ltdocument_and_more')]
    operations = [migrations.RunPython(seed_sequences, migrations.RunPython.noop)]

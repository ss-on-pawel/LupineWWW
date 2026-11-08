from django.db import migrations


def forward(apps, schema_editor):
    Asset = apps.get_model('assets', 'Asset')
    Event = apps.get_model('assets', 'AssetWithdrawal')
    alias = schema_editor.connection.alias
    Event.objects.using(alias).bulk_create([
        Event(asset_id=pk, method='unknown', is_historical=True,
              notes='Stan archiwalny sprzed wprowadzenia rejestru wycofań. Przyczyna i data nieustalone.',
              source_reference='migration:0035')
        for pk in Asset.objects.using(alias).filter(is_active=False).values_list('pk', flat=True)
    ], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [('assets', '0034_withdrawal_events')]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]

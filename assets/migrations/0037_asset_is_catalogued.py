from django.db import migrations, models


class AddFieldPreservingTriggers(migrations.AddField):
    """SQLite rebuilds tables for indexed fields; retain barcode guards."""
    def _run(self, operation, app_label, schema_editor, from_state, to_state):
        triggers = []
        if schema_editor.connection.vendor == 'sqlite':
            with schema_editor.connection.cursor() as cursor:
                cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='assets_asset'")
                triggers = cursor.fetchall()
        operation(app_label, schema_editor, from_state, to_state)
        for name, sql in triggers:
            with schema_editor.connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=%s", [name])
                exists = cursor.fetchone()
            if not exists:
                schema_editor.execute(sql)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self._run(super().database_forwards, app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self._run(super().database_backwards, app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    dependencies = [('assets', '0036_asset_identifier_requirements')]
    operations = [AddFieldPreservingTriggers(
        model_name='asset', name='is_catalogued',
        field=models.BooleanField(default=False, db_index=True, verbose_name='Katalogowany',
            help_text='Etykieta przechowywana w segregatorze zamiast na przedmiocie.'))]

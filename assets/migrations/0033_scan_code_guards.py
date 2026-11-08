from django.db import migrations


def install(apps, schema_editor):
    ScanCode = apps.get_model('assets', 'ScanCode')
    Asset = apps.get_model('assets', 'Asset')
    Location = apps.get_model('locations', 'Location')
    alias = schema_editor.connection.alias
    rows = {}
    for model, field, owner in ((Asset, 'barcode', 'asset_id'), (Location, 'code', 'location_id')):
        for pk, code in model.objects.using(alias).exclude(**{field: ''}).values_list('pk', field).iterator():
            rows.setdefault(code, {'code': code})[owner] = pk
    ScanCode.objects.using(alias).bulk_create([ScanCode(**row) for row in rows.values()], batch_size=500)
    vendor = schema_editor.connection.vendor
    for table, field, owner, other in (
        ('assets_asset', 'barcode', 'asset_id', 'location_id'),
        ('locations_location', 'code', 'location_id', 'asset_id'),
    ):
        name = 'claim_' + table + '_scan_code'
        if vendor == 'postgresql':
            schema_editor.execute(f'''
                CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP = 'DELETE' THEN
                        UPDATE assets_scancode SET {owner} = NULL WHERE {owner} = OLD.id;
                        DELETE FROM assets_scancode WHERE asset_id IS NULL AND location_id IS NULL;
                        RETURN OLD;
                    END IF;
                    IF TG_OP = 'UPDATE' THEN
                        IF OLD.{field} IS NOT DISTINCT FROM NEW.{field} THEN RETURN NEW; END IF;
                    END IF;
                    IF NEW.{field} IS NULL OR NEW.{field} = '' THEN RETURN NEW; END IF;
                    INSERT INTO assets_scancode(code, {owner}) VALUES(NEW.{field}, NEW.id)
                    ON CONFLICT(code) DO UPDATE SET {owner} = EXCLUDED.{owner}
                    WHERE assets_scancode.{owner} = EXCLUDED.{owner} AND assets_scancode.{other} IS NULL;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'scan_code_global_unique' USING ERRCODE = '23505', CONSTRAINT = 'scan_code_global_unique';
                    END IF;
                    RETURN NEW;
                END; $$;
                CREATE TRIGGER {name} AFTER INSERT OR UPDATE OF {field} OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION {name}();
            ''')
        elif vendor == 'sqlite':
            claim = f'''
                INSERT INTO assets_scancode(code, {owner}) VALUES(NEW.{field}, NEW.id)
                ON CONFLICT(code) DO UPDATE SET {owner} = excluded.{owner}
                WHERE assets_scancode.{owner} = excluded.{owner} AND assets_scancode.{other} IS NULL;
                SELECT RAISE(ABORT, 'scan_code_global_unique') WHERE changes() = 0;
            '''
            schema_editor.execute(f'''CREATE TRIGGER {name}_insert AFTER INSERT ON {table}
                WHEN NEW.{field} != '' BEGIN {claim} END;''')
            schema_editor.execute(f'''CREATE TRIGGER {name}_update AFTER UPDATE OF {field} ON {table}
                WHEN NEW.{field} != '' AND OLD.{field} != NEW.{field} BEGIN {claim} END;''')
            schema_editor.execute(f'''CREATE TRIGGER {name}_delete AFTER DELETE ON {table} BEGIN
                UPDATE assets_scancode SET {owner} = NULL WHERE {owner} = OLD.id;
                DELETE FROM assets_scancode WHERE asset_id IS NULL AND location_id IS NULL;
            END;''')
        else:
            raise RuntimeError('Shared scan code guards require PostgreSQL or SQLite')


def uninstall(apps, schema_editor):
    for table in ('assets_asset', 'locations_location'):
        name = 'claim_' + table + '_scan_code'
        if schema_editor.connection.vendor == 'postgresql':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name} ON {table}')
            schema_editor.execute(f'DROP FUNCTION IF EXISTS {name}()')
        else:
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}_insert')
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}_update')
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}_delete')


class Migration(migrations.Migration):
    dependencies = [('assets', '0032_shared_scan_codes'), ('locations', '0003_shared_scan_codes')]
    operations = [migrations.RunPython(install, uninstall)]

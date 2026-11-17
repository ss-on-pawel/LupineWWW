from django.db import migrations


def replace_release(schema_editor, optimized):
    for table, field, owner, other in (
        ('assets_asset','barcode','asset_id','location_id'),
        ('locations_location','code','location_id','asset_id')):
        name='claim_'+table+'_scan_code'
        if optimized:
            release=f'''DELETE FROM assets_scancode WHERE {owner}=OLD.id AND {other} IS NULL;
                UPDATE assets_scancode SET {owner}=NULL WHERE {owner}=OLD.id;'''
        else:
            release=f'''UPDATE assets_scancode SET {owner}=NULL WHERE {owner}=OLD.id;
                DELETE FROM assets_scancode WHERE asset_id IS NULL AND location_id IS NULL;'''
        if schema_editor.connection.vendor=='postgresql':
            schema_editor.execute(f'''CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP='DELETE' THEN {release} RETURN OLD; END IF;
                    IF TG_OP='UPDATE' THEN
                        IF OLD.{field} IS NOT DISTINCT FROM NEW.{field} THEN RETURN NEW; END IF;
                    END IF;
                    IF NEW.{field} IS NULL OR NEW.{field}='' THEN RETURN NEW; END IF;
                    INSERT INTO assets_scancode(code,{owner}) VALUES(NEW.{field},NEW.id)
                    ON CONFLICT(code) DO UPDATE SET {owner}=EXCLUDED.{owner}
                    WHERE assets_scancode.{owner}=EXCLUDED.{owner} AND assets_scancode.{other} IS NULL;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'scan_code_global_unique' USING ERRCODE='23505', CONSTRAINT='scan_code_global_unique';
                    END IF;
                    RETURN NEW;
                END; $$;''')
        elif schema_editor.connection.vendor=='sqlite':
            schema_editor.execute(f'DROP TRIGGER IF EXISTS {name}_delete')
            schema_editor.execute(f'CREATE TRIGGER {name}_delete AFTER DELETE ON {table} BEGIN {release} END;')
        else:
            raise RuntimeError('Shared scan code guards require PostgreSQL or SQLite')


def forward(apps,schema_editor):replace_release(schema_editor,True)
def backward(apps,schema_editor):replace_release(schema_editor,False)


class Migration(migrations.Migration):
    dependencies=[('assets','0040_assetimportreceipt')]
    operations=[migrations.RunPython(forward,backward)]

from django.db import migrations

TABLES = ('organization','contact','lead','deal','task','note','attachment','auditevent','importrun')

def apply_security(apps,schema_editor):
    if schema_editor.connection.vendor!='postgresql': return
    for name in TABLES:
        table=f'crmapp_{name}'
        schema_editor.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        schema_editor.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        expression="workspace_id = NULLIF(current_setting('crm.workspace_id', true), '')::uuid"
        schema_editor.execute(f'CREATE POLICY workspace_isolation ON {table} USING ({expression}) WITH CHECK ({expression})')
        model=apps.get_model('crmapp',name)
        for field in model._meta.fields:
            if field.is_relation and field.name!='workspace' and any(f.name=='workspace' for f in field.related_model._meta.fields):
                target=field.related_model._meta.db_table
                schema_editor.execute(f'ALTER TABLE {table} ADD CONSTRAINT {name}_{field.name}_same_workspace FOREIGN KEY (workspace_id, {field.column}) REFERENCES {target} (workspace_id, id) DEFERRABLE INITIALLY IMMEDIATE')

def remove_security(apps,schema_editor):
    if schema_editor.connection.vendor!='postgresql': return
    for name in TABLES:
        table=f'crmapp_{name}'
        schema_editor.execute(f'DROP POLICY workspace_isolation ON {table}')
        schema_editor.execute(f'ALTER TABLE {table} DISABLE ROW LEVEL SECURITY')
        model=apps.get_model('crmapp',name)
        for field in model._meta.fields:
            if field.is_relation and field.name!='workspace' and any(f.name=='workspace' for f in field.related_model._meta.fields):
                schema_editor.execute(f'ALTER TABLE {table} DROP CONSTRAINT {name}_{field.name}_same_workspace')

class Migration(migrations.Migration):
    dependencies=[('crmapp','0001_initial')]
    operations=[migrations.RunPython(apply_security,remove_security)]

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Fail unless the current PostgreSQL application role and business RLS are safe."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("PostgreSQL is required for database isolation.")
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            if any(cursor.fetchone()):
                raise CommandError("Application role has superuser or BYPASSRLS privileges.")
            cursor.execute(
                "SELECT relname,relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner)=current_user FROM pg_class WHERE relname IN ('crmapp_contact','crmapp_organization','crmapp_lead','crmapp_deal','crmapp_task','crmapp_note','crmapp_attachment','crmapp_auditevent','crmapp_importrun')"
            )
            rows = cursor.fetchall()
            if len(rows) != 9 or any(
                not enabled or not forced or owner for _, enabled, forced, owner in rows
            ):
                raise CommandError("Missing RLS or application owns business tables.")
            cursor.execute(
                "SELECT has_table_privilege(current_user,'crmapp_auditevent','UPDATE'),has_table_privilege(current_user,'crmapp_auditevent','DELETE')"
            )
            if any(cursor.fetchone()):
                raise CommandError("Application must not mutate existing audit events.")
        self.stdout.write(
            self.style.SUCCESS("Restricted runtime role and all nine business-table RLS settings verified.")
        )

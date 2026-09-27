from django.test.runner import DiscoverRunner
from django.db import connection


class PostgresRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        result = super().setup_databases(**kwargs)
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='crm_test_runtime'")
                if not cursor.fetchone():
                    cursor.execute("CREATE ROLE crm_test_runtime NOLOGIN NOSUPERUSER NOBYPASSRLS")
                cursor.execute("GRANT USAGE ON SCHEMA public TO crm_test_runtime")
                cursor.execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_test_runtime"
                )
                cursor.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_test_runtime")
                cursor.execute("REVOKE UPDATE, DELETE ON crmapp_auditevent FROM crm_test_runtime")
        return result

-- Run as the migration owner AFTER migrations. Create crm_runtime separately
-- with a unique password in the provider console. Never grant it owner membership.
ALTER ROLE crm_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO crm_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO crm_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO crm_runtime;
REVOKE UPDATE, DELETE ON crmapp_auditevent FROM crm_runtime;
-- Reapply after adding tables. Defaults are deliberately not permissive.

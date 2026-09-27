# Folio CRM

A Django 5.2 LTS / PostgreSQL CRM for separate company workspaces. Bootstrap is vendored locally; there are no CDN dependencies at runtime. This is an implemented local base release, not a deployed production service.

## Open this prepared local installation

The app runs at **http://127.0.0.1:8000** while the local server is active. See `LOCAL_ACCESS.md` for the synthetic demo credentials. The demo is a Member account so it can be explored without enrolling an authenticator. Company owners and admins must enroll MFA.

To restart on this computer, open PowerShell in this folder and run:

```powershell
.\Start-Local.ps1
```

This uses the prepared Python environment and PostgreSQL cluster in the sibling session `work/` directory. PostgreSQL listens only on `127.0.0.1:55432`; the application listens only on loopback. Keep the PowerShell window open. Verification/reset/invitation emails are printed to that terminal in local development. No real emails are sent.

To test the complete owner journey, sign out, register a workspace, open its verification link from the terminal, sign in, and enroll an authenticator using the displayed setup key. Set timezone/currency, invite staff, add/import contacts, create a lead, and convert it to a deal. The pilot default is three active companies. Each login email belongs to only one workspace.

The demo records and the generated recovery-test image are synthetic. Do not put real customer data into this local development setup.

## Implemented

- Self-service registration, single-use email verification, company activation and resumable onboarding checklist.
- Email/password login, password reset, owner/admin MFA with encrypted TOTP secrets and replay protection, session invalidation, invitation expiry/revocation, owner transfer, staff deactivation, and operator MFA recovery.
- Owner/Admin/Member authorization, workspace-scoped queries, PostgreSQL forced row-level security on nine business tables, and composite foreign keys preventing cross-company relationships.
- Organizations, contacts, leads, deals, assignees, archive/restore, duplicate email warnings, optimistic edit concurrency, and idempotent transactional lead conversion.
- Deal board and accessible table view, filtering, search, pagination, won value and pipeline totals.
- Tasks, due/overdue views, task editing/completion, and plain-text notes.
- CSV templates, validation and preview, signed time-limited confirmation, all-or-nothing batch import, repeated-file protection, and formula-safe export.
- Private attachment storage adapters (local development / S3-compatible production), type/size checks, workspace storage quota, authenticated downloads, and a fail-closed ClamAV command integration.
- Append-only runtime audit events, encrypted transactional-email outbox, retries, health check, Docker/Render configuration, PostgreSQL CI, and encrypted backup/empty-database restore scripts.

## Current limitations and production gates

**Attachments are disabled until a real scanner is configured.** Set `FILE_SCAN_COMMAND` to a ClamAV-compatible executable with current signatures. The integration is tested with controlled scanner outcomes; no real malware scanner is installed in this session. The trusted generated demo image is a recovery fixture, not evidence of a scanned upload.

There are no hosting accounts, verified email sender, live S3 bucket, scheduled operational runner, or production deployment configured. External email/S3 delivery and provider spending limits remain unverified. Local backups are not independent off-host backups. Actual concurrency/capacity targets and a multi-company pilot observation period remain outstanding. Do not describe this installation as production approved.

The board shows up to 30 deals per stage, with a link to the paginated full list. CSV import uses exact provided headers; field-mapping UI and row-level merge/upsert are not included. Identical files are idempotent, but modified files may contain duplicates and should be reviewed. Notes are add-only. Reports use one workspace currency and do not perform exchange-rate conversion.

The shared control-plane tables for accounts, sessions, invitations and email delivery use narrowly scoped application access; business-table RLS is not a substitute for protecting database/admin credentials. Operators with backup or migration credentials are trusted. Per-company restoration requires a separate database restore and carefully validated tenant extraction.

## Standard installation elsewhere

Requirements: Python 3.12+, PostgreSQL 17 (or a compatible supported version), and separate database owner/runtime roles.

```sh
python -m venv .venv
# Activate .venv using the appropriate command for your operating system.
pip install -r requirements.txt
```

Set environment variables from `.env.example` through your shell or secret manager. The app intentionally does not silently load `.env`. Generate `SECRET_KEY` with `secrets.token_urlsafe(48)` and `FIELD_ENCRYPTION_KEY` with `cryptography.fernet.Fernet.generate_key()`. Keep the encryption key stable: changing it without a migration makes existing MFA secrets and queued emails unreadable.

1. Create the database and migration owner; create `crm_runtime` as a separate login with no superuser, role-creation, database-creation, owner membership, or BYPASSRLS privileges.
2. Temporarily set `DATABASE_URL` to the migration owner, then run `python manage.py migrate`.
3. As the owner, apply `scripts/grant_runtime.sql`. Do not grant table ownership to the application.
4. Change `DATABASE_URL` to `crm_runtime` and run `python manage.py verify_isolation`.
5. For local development, set `DEBUG=true`, `PUBLIC_URL=http://127.0.0.1:8000` and run `python manage.py runserver 127.0.0.1:8000`.
6. For production, set all required secrets, HTTPS host/origin values and storage/scanner settings, run deployment checks and release gates, and use Gunicorn through the container. Never run the development server publicly.

SQLite is available only as an explicit local DEBUG fallback. It does not provide RLS and is not the verified deployment path. Production refuses SQLite. `verify_isolation` also rejects a superuser, BYPASSRLS role, table owner, missing forced RLS, or mutable runtime audit history.

## Verification

```sh
ruff check .
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
python manage.py collectstatic --noinput
```

Run tests using a disposable PostgreSQL instance and a test-administrator connection. The custom test runner creates `crm_test_runtime`; tests switch into this non-owner, non-superuser role before exercising the app. Do not run this test runner against production. CI uses a disposable PostgreSQL 17 service. Test fixtures never require access to customer data.

`RECOVERY_TEST.json` records the local recovery rehearsal. `TEST_REPORT.md` records verified behavior and remaining checks. Code changes require rerunning the relevant tests.

## Operational commands

Schedule `python manage.py retry_email` every 15 minutes using a trusted runner. Retries are persistent and bounded; exhausted delivery returns a failure exit code. Monitor scheduler failures and queue backlog. Set Resend paid overages off in the provider account; the application's conservative daily limiter is an additional guard, not a billing guarantee.

For a recovery rehearsal:

```sh
# Supply BACKUP_DATABASE_URL, BACKUP_ENCRYPTION_KEY and PG_DUMP securely.
python scripts/backup.py --output /independent/secure/location/backup.fernet
# Supply RESTORE_DATABASE_URL for a NEW EMPTY database and PG_RESTORE.
python scripts/restore.py --input /independent/secure/location/backup.fernet --files-output /new/restore-files
```

The backup takes a PostgreSQL exported snapshot, includes all referenced immutable attachments, verifies file hashes, and encrypts the archive. Keep the encryption key separately. Store seven daily backups off-host; monitor age and failed/missed runs. Restore refuses a populated database or nonempty file destination. After restore, apply runtime grants, run isolation checks and application smoke tests, verify row/file counts and ownership, then deliberately switch service configuration. Never restore over the live database.

For MFA loss, an operator must independently verify the account owner and record the decision in the secured incident system before running:

```sh
python manage.py reset_mfa --email VERIFIED-ACCOUNT --case-reference INCIDENT-ID
```

This revokes existing sessions and forces privileged-account re-enrollment. It does not reset the password. The application records the recovery action without logging authenticator secrets.

## Launch checklist

- Verify real external-recipient email delivery, domain authentication, private storage access, malware scanning, and failure behavior.
- Confirm provider allocations and spending controls; provision alerts and pause onboarding when limits approach. No automatic paid upgrade.
- Configure off-host scheduled backups and retry runner, rehearse recovery on the deployed services, and verify alert delivery.
- Run `check --deploy`, `verify_isolation`, the PostgreSQL test suite, dependency/security review, and load testing with representative data and concurrent users.
- Choose the accountable operator and support channel; publish the applicable privacy/retention notice and define customer export/deletion handling.
- Start with 2–3 companies and observe a stable week before expansion. No uptime guarantee is offered by the free deployment configuration.

Advanced features remain outside this base release as agreed.

# Local verification report

Date: 26 September 2026. Application: Folio CRM base release.

## Automated checks

- **40 tests passed** on PostgreSQL 17.11, with all application test requests running as `crm_test_runtime`, a non-owner/non-superuser role without BYPASSRLS.
- Lint passed (`ruff check`).
- Django system checks passed; no pending model migrations were detected.
- Runtime isolation verification passed: forced RLS on all nine business tables, no privileged runtime role, and no runtime audit update/delete privileges.
- Static asset collection passed. Bootstrap and app styles are served locally.
- Django `check --deploy --fail-level WARNING` passed with production settings; this checks application configuration, not external service readiness.

Security coverage includes raw SQL reads without context, cross-workspace writes and foreign keys, pooled/nested context cleanup, cross-company detail/edit/archive/download attempts, forged workspace/assignee/organization values, role checks, deactivation, MFA requirements and replay rejection, CSRF, verification/invitation expiry and single-use behavior, and prevention of suspended-workspace reactivation using an old verification token.

Workflow coverage includes lead conversion idempotency and contact reuse, quota rollback, optimistic edits, HTML escaping, CSV preview/atomic commit/idempotency/signature validation/formula safety, archive filtering, deal-board rendering, task access control, and scanner fail-closed/clean/infected outcome handling. Scanner responses in automated tests are mocked; this does not verify a real scanner installation.

## Recovery rehearsal

Encrypted a PostgreSQL snapshot and one generated, trusted synthetic attachment. Restored to a new empty database and fresh file directory. Matched workspace, account, contact, lead, deal, task, audit, and attachment counts; verified the restored attachment hash; reapplied runtime grants and confirmed missing-workspace raw queries return no business records. All nine forced-RLS settings survived the restore.

See `RECOVERY_TEST.json` for measured counts and local elapsed time. The small synthetic dataset restored in approximately three seconds; this is not a recovery-time promise for production volumes. Backup files and encryption keys remain in the session's local `work/` area, not in the source package.

## Browser inspection

Signed in through the real local login form and inspected the dashboard, contacts, deal table, and deal board. Checked desktop (1366 px) and mobile (390 px) breakpoints. Contacts and board have contained horizontal scrolling rather than page-wide overflow. The mobile account/settings menu was added after inspection revealed that desktop-only controls were hidden on small screens.

## Not yet verified

- Real transactional email to external recipients, a verified sending domain, and email-provider failure/delivery webhooks.
- A real malware scanner/signature-update process and live S3-compatible storage.
- Scheduled off-host backups and alert delivery.
- Production hosting, container execution on the target provider, spending controls, concurrency/load targets, and 25-company operation.
- Independent security review and a monitored real-company pilot.

The installation is suitable for local evaluation with synthetic data. These outstanding checks prevent a production-ready claim.

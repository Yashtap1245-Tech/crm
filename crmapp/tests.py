import csv
import io
from unittest.mock import patch
import pyotp
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (
    Account,
    ActionToken,
    Attachment,
    AuditEvent,
    Contact,
    Deal,
    EmailOutbox,
    Lead,
    Organization,
    Task,
    Workspace,
)
from .security import encrypt, verify_totp
from .services import convert_lead, token_for
from .tenancy import workspace_context

PASSWORD = "Correct horse battery staple 93!"


@override_settings(
    FIELD_ENCRYPTION_KEY="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CRMTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a = Workspace.objects.create(name="Company Alpha")
        cls.b = Workspace.objects.create(name="Company Beta")
        cls.owner = Account.objects.create_user(
            username="owner@alpha.example",
            email="owner@alpha.example",
            password=PASSWORD,
            workspace=cls.a,
            role="owner",
            verified=True,
            totp_secret=encrypt(pyotp.random_base32()),
        )
        cls.member = Account.objects.create_user(
            username="member@alpha.example",
            email="member@alpha.example",
            password=PASSWORD,
            workspace=cls.a,
            role="member",
            verified=True,
        )
        cls.other = Account.objects.create_user(
            username="owner@beta.example",
            email="owner@beta.example",
            password=PASSWORD,
            workspace=cls.b,
            role="owner",
            verified=True,
            totp_secret=encrypt(pyotp.random_base32()),
        )
        with workspace_context(cls.a.pk):
            cls.contact = Contact.objects.create(
                workspace=cls.a, name="Alpha Contact", email="alpha@example.com", assigned_to=cls.member
            )
            cls.lead = Lead.objects.create(workspace=cls.a, name="Alpha Lead", assigned_to=cls.member)
            cls.org = Organization.objects.create(workspace=cls.a, name="Alpha Org")
        with workspace_context(cls.b.pk):
            cls.hidden = Contact.objects.create(
                workspace=cls.b, name="Beta Secret Contact", email="secret@example.com", assigned_to=cls.other
            )
            cls.hidden_org = Organization.objects.create(workspace=cls.b, name="Beta Org")
            cls.hidden_file = Attachment.objects.create(
                workspace=cls.b,
                contact=cls.hidden,
                creator=cls.other,
                name="private.pdf",
                key="private.pdf",
                size=1,
                content_type="application/pdf",
                sha256="a" * 64,
            )

    def setUp(self):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET ROLE crm_test_runtime")
        self.sign_in(self.owner)

    def tearDown(self):
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")

    def sign_in(self, user):
        self.client.force_login(user, backend="crmapp.auth.EmailBackend")
        session = self.client.session
        session["mfa_user"] = user.pk
        session["session_version"] = user.session_version
        session.save()

    def test_public_routes_and_authenticated_screens(self):
        for path in [
            "/",
            "/onboarding/",
            "/team/",
            "/tasks/",
            "/audit/",
            "/records/contacts/",
            "/records/organizations/",
            "/records/leads/",
            "/records/deals/",
            "/records/contacts/new/",
            f"/records/contacts/{self.contact.pk}/",
            "/records/contacts/import/",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.client.logout()
        for path in ["/login/", "/signup/", "/password/reset/", "/health/"]:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_contact_search_excludes_other_workspace(self):
        response = self.client.get("/records/contacts/?q=Secret")
        self.assertNotContains(response, "Beta Secret Contact")
        self.assertEqual(response.context["page"].paginator.count, 0)

    def test_cross_workspace_detail_update_archive_files(self):
        for method, url, data in [
            ("get", f"/records/contacts/{self.hidden.pk}/", {}),
            ("post", f"/records/contacts/{self.hidden.pk}/edit/", {"name": "hacked"}),
            ("post", f"/records/contacts/{self.hidden.pk}/archive/", {}),
            ("get", f"/files/{self.hidden_file.pk}/", {}),
        ]:
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url, data).status_code, 404)

    def test_export_does_not_include_other_workspace(self):
        response = self.client.get("/records/contacts/export/")
        self.assertContains(response, "Alpha Contact")
        self.assertNotContains(response, "Beta Secret Contact")

    def test_forged_workspace_field_is_ignored(self):
        response = self.client.post(
            "/records/contacts/new/",
            {"name": "New alpha", "workspace": self.b.pk, "assigned_to": self.member.pk},
        )
        self.assertEqual(response.status_code, 302)
        with workspace_context(self.a.pk):
            self.assertTrue(Contact.objects.filter(name="New alpha", workspace=self.a).exists())
        with workspace_context(self.b.pk):
            self.assertFalse(Contact.objects.filter(name="New alpha").exists())

    def test_forged_assignee_and_organization_are_invalid(self):
        response = self.client.post(
            "/records/contacts/new/",
            {"name": "Bad reference", "assigned_to": self.other.pk, "organization": self.hidden_org.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("assigned_to", response.context["form"].errors)
        self.assertIn("organization", response.context["form"].errors)

    def test_missing_context_manager_returns_no_records(self):
        self.assertEqual(Contact.objects.count(), 0)

    def test_raw_database_read_without_context_is_denied(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL RLS required")
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM crmapp_contact")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_raw_database_read_is_scoped(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL RLS required")
        with workspace_context(self.a.pk), connection.cursor() as cursor:
            cursor.execute("SELECT name FROM crmapp_contact")
            self.assertEqual(cursor.fetchall(), [("Alpha Contact",)])

    def test_raw_wrong_workspace_write_rejected(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL RLS required")
        with workspace_context(self.a.pk):
            with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE crmapp_contact SET workspace_id=%s WHERE id=%s", [self.b.pk, self.contact.pk]
                )

    def test_cross_workspace_foreign_key_rejected_by_database(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        with workspace_context(self.a.pk):
            with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE crmapp_contact SET organization_id=%s WHERE id=%s",
                    [self.hidden_org.pk, self.contact.pk],
                )

    def test_connection_reuse_and_nested_context(self):
        with workspace_context(self.a.pk):
            self.assertEqual(Contact.objects.get().name, "Alpha Contact")
            with workspace_context(self.b.pk):
                self.assertEqual(Contact.objects.get().name, "Beta Secret Contact")
            self.assertEqual(Contact.objects.get().name, "Alpha Contact")
        self.assertEqual(Contact.objects.count(), 0)
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM crmapp_contact")
                self.assertEqual(cursor.fetchone()[0], 0)

    def test_member_admin_actions_denied(self):
        self.sign_in(self.member)
        for method, url in [
            ("get", "/team/"),
            ("get", "/audit/"),
            ("get", "/records/contacts/export/"),
            ("get", "/records/contacts/import/"),
            ("post", f"/records/contacts/{self.contact.pk}/archive/"),
        ]:
            self.assertEqual(getattr(self.client, method)(url).status_code, 403)

    def test_owner_cannot_be_deactivated(self):
        self.assertEqual(
            self.client.post(f"/team/{self.owner.pk}/", {"action": "deactivate"}).status_code, 403
        )

    def test_deactivation_revokes_existing_session(self):
        self.sign_in(self.member)
        Account.objects.filter(pk=self.member.pk).update(is_active=False)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_mfa_required_for_owner(self):
        session = self.client.session
        session.pop("mfa_user", None)
        session.save()
        self.assertRedirects(self.client.get("/"), "/mfa/verify/", fetch_redirect_response=False)

    def test_mfa_code_cannot_be_replayed(self):
        secret = pyotp.random_base32()
        self.owner.totp_secret = encrypt(secret)
        self.owner.save(update_fields=["totp_secret"])
        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_totp(self.owner, code))
        self.assertFalse(verify_totp(self.owner, code))

    def test_mfa_setup_accepts_adjacent_steps_and_records_replay_boundary(self):
        now = 1800000015
        for offset in (-1, 0, 1):
            with self.subTest(offset=offset):
                Account.objects.filter(pk=self.owner.pk).update(totp_secret="", totp_last_step=-1)
                self.client.get("/mfa/setup/")
                from .security import decrypt
                secret = decrypt(self.client.session["pending_totp"])
                code = pyotp.TOTP(secret).at(now + offset * 30)
                with patch("crmapp.security.time.time", return_value=now):
                    response = self.client.post("/mfa/setup/", {"code": code})
                    self.assertRedirects(response, "/onboarding/", fetch_redirect_response=False)
                    self.owner.refresh_from_db()
                    self.assertEqual(self.owner.totp_last_step, now // 30 + offset)
                    self.assertFalse(verify_totp(self.owner, code))
                self.assertNotIn("pending_totp", self.client.session)

    def test_mfa_setup_rejects_stale_and_unrelated_codes_without_enrolling(self):
        from .security import decrypt
        Account.objects.filter(pk=self.owner.pk).update(totp_secret="", totp_last_step=-1)
        self.client.get("/mfa/setup/")
        secret = decrypt(self.client.session["pending_totp"])
        now = 1800000015
        codes = [pyotp.TOTP(secret).at(now - 90), pyotp.TOTP(secret).at(now + 90), "abcdef"]
        for code in codes:
            with patch("crmapp.security.time.time", return_value=now):
                response = self.client.post("/mfa/setup/", {"code": code})
            self.assertContains(response, "Code not accepted")
            self.owner.refresh_from_db()
            self.assertEqual(self.owner.totp_secret, "")
            self.assertEqual(self.owner.totp_last_step, -1)
            self.assertEqual(decrypt(self.client.session["pending_totp"]), secret)

    def test_conversion_is_idempotent(self):
        with workspace_context(self.a.pk):
            first = convert_lead(self.owner, self.lead.pk)
            second = convert_lead(self.owner, self.lead.pk)
            self.assertEqual(first.pk, second.pk)
            self.assertEqual(Deal.objects.count(), 1)
            self.assertEqual(Contact.objects.count(), 2)
            self.lead.refresh_from_db()
            self.assertEqual(self.lead.status, "qualified")

    def test_optimistic_concurrency(self):
        payload = {"name": "First edit", "assigned_to": self.member.pk, "version": 1}
        self.assertEqual(
            self.client.post(f"/records/contacts/{self.contact.pk}/edit/", payload).status_code, 302
        )
        payload["name"] = "Stale edit"
        response = self.client.post(f"/records/contacts/{self.contact.pk}/edit/", payload)
        self.assertContains(response, "Someone changed this record")
        with workspace_context(self.a.pk):
            self.assertEqual(Contact.objects.get(pk=self.contact.pk).name, "First edit")

    def test_notes_escape_html(self):
        self.client.post(
            f"/records/contacts/{self.contact.pk}/activity/",
            {"action": "note", "body": "<script>alert(1)</script>"},
        )
        response = self.client.get(f"/records/contacts/{self.contact.pk}/")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_csv_formula_neutralization(self):
        with workspace_context(self.a.pk):
            Contact.objects.create(workspace=self.a, name='=HYPERLINK("evil")', phone="+12345")
        response = self.client.get("/records/contacts/export/")
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        risky = next(row for row in rows if "HYPERLINK" in row["name"])
        self.assertTrue(risky["name"].startswith("'="))
        self.assertTrue(risky["phone"].startswith("'+"))

    def test_csv_import_preview_and_idempotency(self):
        upload = SimpleUploadedFile(
            "contacts.csv", b"name,email,phone,source\nNew Contact,new@example.com,123,Referral\n"
        )
        response = self.client.post("/records/contacts/import/", {"file": upload})
        signed = response.context["signed"]
        self.assertIsNotNone(signed)
        with workspace_context(self.a.pk):
            self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(
            self.client.post("/records/contacts/import/", {"confirmed": signed}).status_code, 302
        )
        self.assertEqual(
            self.client.post("/records/contacts/import/", {"confirmed": signed}).status_code, 302
        )
        with workspace_context(self.a.pk):
            self.assertEqual(Contact.objects.count(), 2)

    def test_csv_invalid_row_prevents_import(self):
        upload = SimpleUploadedFile(
            "contacts.csv", b"name,email,phone,source\nGood,good@example.com,,\nBad,not-an-email,,\n"
        )
        response = self.client.post("/records/contacts/import/", {"file": upload})
        self.assertIsNone(response.context["signed"])
        self.assertTrue(response.context["errors"])

    def test_tampered_import_token_rejected(self):
        response = self.client.post("/records/contacts/import/", {"confirmed": "bad-token"})
        self.assertTrue(response.context["errors"])

    @override_settings(FILE_SCAN_COMMAND="")
    def test_uploads_fail_closed_without_scanner(self):
        response = self.client.post(
            f"/records/contacts/{self.contact.pk}/upload/",
            {"file": SimpleUploadedFile("sample.pdf", b"%PDF-1.7\nhello")},
            follow=True,
        )
        self.assertContains(response, "scanner is configured")
        with workspace_context(self.a.pk):
            self.assertEqual(Attachment.objects.count(), 0)

    def test_audit_immutable_for_runtime_role(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL required")
        self.client.post("/records/contacts/new/", {"name": "Audited"})
        with workspace_context(self.a.pk):
            self.assertTrue(AuditEvent.objects.exists())
            with self.assertRaises(DatabaseError), transaction.atomic():
                AuditEvent.objects.all().delete()

    def test_signup_and_verification_are_single_use(self):
        self.client.logout()
        response = self.client.post(
            "/signup/",
            {
                "name": "New Owner",
                "company": "New Company",
                "email": "new@company.example",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        user = Account.objects.get(email="new@company.example")
        self.assertFalse(user.verified)
        self.assertFalse(user.workspace.active)
        with transaction.atomic():
            raw = token_for("verify", user.email, account=user)
        self.assertEqual(self.client.get(f"/verify/{raw}/").status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.verified)
        self.assertEqual(self.client.post(f"/verify/{raw}/").status_code, 302)
        self.assertEqual(self.client.post(f"/verify/{raw}/").status_code, 400)

    def test_expired_invitation_rejected(self):
        with transaction.atomic():
            raw = token_for("invite", "invite@example.com", workspace=self.a)
        ActionToken.objects.filter(email="invite@example.com").update(expires_at=timezone.now())
        self.client.logout()
        self.assertEqual(self.client.get(f"/invite/{raw}/").status_code, 400)

    def test_invitation_creates_account_only_in_target_workspace(self):
        with transaction.atomic():
            raw = token_for("invite", "invite@example.com", workspace=self.a)
        self.client.logout()
        response = self.client.post(
            f"/invite/{raw}/",
            {
                "name": "Invited",
                "email": "evil@example.com",
                "password": PASSWORD,
                "confirm_password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        user = Account.objects.get(email="invite@example.com")
        self.assertEqual(user.workspace_id, self.a.pk)
        self.assertTrue(user.verified)
        self.assertEqual(user.role, "member")

    def test_password_reset_is_queued_and_not_enumerating(self):
        self.client.logout()
        for email in [self.owner.email, "missing@example.com"]:
            self.assertEqual(self.client.post("/password/reset/", {"email": email}).status_code, 302)
        self.assertEqual(EmailOutbox.objects.filter(recipient=self.owner.email).count(), 1)

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get("/logout/").status_code, 405)

    def test_framing_is_denied(self):
        self.assertEqual(self.client.get("/").headers["X-Frame-Options"], "DENY")

    def test_relationship_dropdowns_list_current_company_for_owner_and_member(self):
        with workspace_context(self.a.pk):
            Contact.objects.create(workspace=self.a, name="Archived contact", archived=True)
            Organization.objects.create(workspace=self.a, name="Archived organization", archived=True)
        for user in (self.owner, self.member):
            self.sign_in(user)
            for kind, name, obj in [
                ("contacts", "organization", self.org),
                ("leads", "contact", self.contact),
                ("deals", "contact", self.contact),
            ]:
                with self.subTest(role=user.role, kind=kind):
                    response = self.client.get(f"/records/{kind}/new/")
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, f'<option value="{obj.pk}">{obj.name}</option>', html=True)
                    self.assertNotContains(response, "Beta Secret Contact")
                    self.assertNotContains(response, "Beta Org")
                    self.assertNotContains(response, "Archived contact")
                    self.assertNotContains(response, "Archived organization")

    def test_contact_accepts_organization_from_current_workspace(self):
        response = self.client.post(
            "/records/contacts/new/",
            {"name": "Linked contact", "organization": str(self.org.pk), "assigned_to": self.member.pk},
        )
        self.assertEqual(response.status_code, 302)
        with workspace_context(self.a.pk):
            self.assertEqual(Contact.objects.get(name="Linked contact").organization_id, self.org.pk)

    def test_deal_accepts_contact_and_keeps_it_selected_on_edit(self):
        response = self.client.post(
            "/records/deals/new/",
            {
                "name": "Linked deal",
                "contact": str(self.contact.pk),
                "amount": "100",
                "stage": "qualified",
                "assigned_to": self.member.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        with workspace_context(self.a.pk):
            deal = Deal.objects.get(name="Linked deal")
        response = self.client.get(f"/records/deals/{deal.pk}/edit/")
        self.assertContains(
            response, f'<option value="{self.contact.pk}" selected>{self.contact.name}</option>', html=True
        )

    def test_csrf_required_for_mutations(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.member, backend="crmapp.auth.EmailBackend")
        self.assertEqual(client.post("/records/contacts/new/", {"name": "CSRF"}).status_code, 403)

    @override_settings(WORKSPACE_RECORD_LIMIT=2)
    def test_record_limit_does_not_partially_convert(self):
        with workspace_context(self.a.pk):
            from django.core.exceptions import ValidationError

            with self.assertRaises(ValidationError):
                convert_lead(self.owner, self.lead.pk)
            self.assertEqual(Deal.objects.count(), 0)
            self.assertEqual(Contact.objects.count(), 1)

    def test_archived_records_excluded_from_list(self):
        self.client.post(f"/records/contacts/{self.contact.pk}/archive/")
        self.assertNotContains(self.client.get("/records/contacts/"), "Alpha Contact")
        self.assertContains(self.client.get("/records/contacts/?archived=1"), "Alpha Contact")

    def test_old_verification_cannot_reactivate_suspended_workspace(self):
        raw = token_for("verify", self.owner.email, account=self.owner)
        Workspace.objects.filter(pk=self.a.pk).update(active=False)
        self.client.logout()
        self.assertEqual(self.client.post(f"/verify/{raw}/").status_code, 400)
        self.a.refresh_from_db()
        self.assertFalse(self.a.active)

    def test_conversion_reuses_unambiguous_existing_contact(self):
        with workspace_context(self.a.pk):
            self.lead.email = self.contact.email
            self.lead.save()
            deal = convert_lead(self.owner, self.lead.pk)
            self.assertEqual(deal.contact_id, self.contact.pk)
            self.assertEqual(Contact.objects.count(), 1)

    def test_board_and_task_edit_are_workspace_scoped(self):
        self.assertEqual(self.client.get("/deals/board/").status_code, 200)
        with workspace_context(self.b.pk):
            task = Task.objects.create(
                workspace=self.b,
                contact=self.hidden,
                creator=self.other,
                assigned_to=self.other,
                name="Private task",
                due_date=timezone.localdate(),
            )
        self.assertEqual(self.client.get(f"/tasks/{task.pk}/edit/").status_code, 404)

    @override_settings(FILE_SCAN_COMMAND="scanner")
    def test_infected_file_is_rejected(self):
        from unittest.mock import patch
        from types import SimpleNamespace

        with patch("crmapp.files.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            response = self.client.post(
                f"/records/contacts/{self.contact.pk}/upload/",
                {"file": SimpleUploadedFile("infected.pdf", b"%PDF-1.7\nsynthetic")},
                follow=True,
            )
        self.assertContains(response, "could not be cleared")
        with workspace_context(self.a.pk):
            self.assertEqual(Attachment.objects.count(), 0)

    @override_settings(FILE_SCAN_COMMAND="scanner")
    def test_cleared_file_metadata_is_bound_to_workspace(self):
        from unittest.mock import patch
        from types import SimpleNamespace

        with (
            patch("crmapp.files.subprocess.run", return_value=SimpleNamespace(returncode=0)),
            patch("crmapp.files.write_object"),
        ):
            response = self.client.post(
                f"/records/contacts/{self.contact.pk}/upload/",
                {"file": SimpleUploadedFile("clean.pdf", b"%PDF-1.7\nsynthetic")},
            )
        self.assertEqual(response.status_code, 302)
        with workspace_context(self.a.pk):
            file = Attachment.objects.get()
            self.assertEqual(file.contact_id, self.contact.pk)
            self.assertTrue(file.key.startswith(str(self.a.pk) + "/"))

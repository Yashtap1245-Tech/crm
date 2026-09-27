from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from crmapp.models import Account
from crmapp.services import audit
from crmapp.tenancy import workspace_context


class Command(BaseCommand):
    help = "Operator-only recovery after verifying the account owner through an independent channel."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--case-reference", required=True)

    def handle(self, *args, **options):
        if len(options["case_reference"].strip()) < 6:
            raise CommandError("Supply an incident/case reference of at least six characters.")
        try:
            user = Account.objects.get(email__iexact=options["email"], is_active=True)
        except Account.DoesNotExist:
            raise CommandError("No active account matches this email.")
        with workspace_context(user.workspace_id), transaction.atomic():
            user = Account.objects.select_for_update().get(pk=user.pk)
            user.totp_secret = ""
            user.totp_last_step = -1
            user.session_version += 1
            user.save(update_fields=["totp_secret", "totp_last_step", "session_version"])
            audit(user, "security.mfa_reset", user)
        # The case reference belongs in the operator's secured incident record.
        self.stdout.write(
            "MFA reset; all sessions revoked. Re-enrollment is required on next owner/admin login."
        )

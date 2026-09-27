import hashlib
import secrets
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import connection, transaction
from django.utils import timezone
from .models import ActionToken, AuditEvent, Contact, Deal, EmailOutbox, Lead, Workspace
from .security import allowed, encrypt, decrypt


def audit(user, action, obj):
    AuditEvent.objects.create(
        workspace_id=user.workspace_id,
        actor=user,
        action=action,
        entity=obj._meta.model_name,
        entity_id=str(obj.pk),
    )


def token_for(purpose, email, account=None, workspace=None, role="member"):
    raw = secrets.token_urlsafe(32)
    ActionToken.objects.create(
        digest=hashlib.sha256(raw.encode()).hexdigest(),
        purpose=purpose,
        email=email.lower(),
        account=account,
        workspace=workspace,
        role=role,
        expires_at=timezone.now() + timedelta(hours=24 if purpose == "verify" else 72),
    )
    return raw


def find_token(raw, purpose):
    return (
        ActionToken.objects.select_for_update()
        .filter(
            digest=hashlib.sha256(raw.encode()).hexdigest(),
            purpose=purpose,
            used=False,
            expires_at__gt=timezone.now(),
        )
        .first()
    )


def queue_mail(email, subject, body):
    item = EmailOutbox.objects.create(recipient=email, subject=subject, encrypted_body=encrypt(body))
    transaction.on_commit(lambda: deliver_mail(item.pk))


def deliver_mail(pk):
    # The provider idempotency key also protects retries after an ambiguous timeout.
    try:
        with transaction.atomic():
            item = EmailOutbox.objects.select_for_update().get(pk=pk)
            if item.sent_at or item.attempts >= 8:
                return
            # Conservative provider budget; preserve the final slots for recovery.
            cap = 95 if item.subject.startswith("Reset ") else 80
            if not allowed("email-day:" + timezone.now().date().isoformat(), cap, 86400):
                return
            item.attempts += 1
            item.save(update_fields=["attempts"])
            try:
                EmailMessage(
                    item.subject,
                    decrypt(item.encrypted_body),
                    settings.DEFAULT_FROM_EMAIL,
                    [item.recipient],
                    headers={"X-Idempotency-Key": f"crm-outbox-{item.pk}"},
                ).send(fail_silently=False)
            except Exception:
                return  # Retained for explicit retry; no secrets are logged.
            item.sent_at = timezone.now()
            item.encrypted_body = ""
            item.save(update_fields=["sent_at", "encrypted_body"])
    except EmailOutbox.DoesNotExist:
        pass


def send_verification(user):
    raw = token_for("verify", user.email, account=user)
    queue_mail(
        user.email,
        "Verify your CRM account",
        f"Confirm your email address:\n{settings.PUBLIC_URL}/verify/{raw}/\nThis link expires in 24 hours.",
    )


def company_capacity_lock():
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(77421931)")
    if Workspace.objects.filter(active=True).count() >= settings.PILOT_COMPANY_LIMIT:
        raise ValidationError("The pilot is at capacity. Please contact support before registering.")


def ensure_record_capacity(workspace):
    Workspace.objects.select_for_update().get(pk=workspace.pk)
    if Contact.objects.count() + Lead.objects.count() >= settings.WORKSPACE_RECORD_LIMIT:
        raise ValidationError("This workspace has reached its pilot record limit.")


@transaction.atomic
def convert_lead(user, pk):
    lead = Lead.objects.select_for_update().get(pk=pk, archived=False)
    existing = Deal.objects.filter(source_lead=lead).first()
    if existing:
        return existing
    if lead.status == "disqualified":
        raise ValidationError("Reopen this lead before conversion.")
    contact = lead.contact
    if not contact and lead.email:
        matches = list(Contact.objects.filter(email__iexact=lead.email, archived=False)[:2])
        if len(matches) > 1:
            raise ValidationError(
                "Several contacts share this email. Select a contact on the lead before conversion."
            )
        if matches:
            contact = matches[0]
    if not contact:
        ensure_record_capacity(user.workspace)
        contact = Contact.objects.create(
            workspace=user.workspace,
            name=lead.name,
            email=lead.email,
            phone=lead.phone,
            source=lead.source,
            assigned_to=lead.assigned_to,
        )
    deal = Deal.objects.create(
        workspace=user.workspace,
        name=lead.name,
        contact=contact,
        source_lead=lead,
        currency=user.workspace.currency,
        assigned_to=lead.assigned_to,
    )
    lead.contact, lead.status = contact, "qualified"
    lead.version += 1
    lead.save()
    audit(user, "lead.converted", deal)
    return deal

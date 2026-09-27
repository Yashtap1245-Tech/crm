import uuid
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from .tenancy import TenantManager, current_workspace


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    timezone = models.CharField(max_length=80, default="UTC")
    currency = models.CharField(max_length=3, default="USD")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Account(AbstractUser):
    username = models.CharField(max_length=254, unique=True)
    workspace = models.ForeignKey(Workspace, null=True, on_delete=models.PROTECT)
    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=10,
        choices=[("owner", "Owner"), ("admin", "Admin"), ("member", "Member")],
        default="member",
    )
    verified = models.BooleanField(default=False)
    totp_secret = models.TextField(blank=True)
    totp_last_step = models.BigIntegerField(default=-1)
    session_version = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="account_email_ci_unique"),
            models.UniqueConstraint(
                fields=["workspace"], condition=Q(role="owner", is_active=True), name="one_active_owner"
            ),
            models.UniqueConstraint(fields=["workspace", "id"], name="account_tenant_key"),
        ]

    @property
    def can_manage(self):
        return self.role in ("owner", "admin")

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        self.username = self.email
        super().save(*args, **kwargs)

    def __str__(self):
        return self.get_full_name() or self.email


class TenantModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()

    class Meta:
        abstract = True
        constraints = [models.UniqueConstraint(fields=["workspace", "id"], name="%(class)s_tenant_key")]
        indexes = [models.Index(fields=["workspace", "created_at"])]

    def clean(self):
        super().clean()
        if not current_workspace.get() or self.workspace_id != current_workspace.get():
            raise ValidationError("A valid workspace context is required.")
        for field in self._meta.fields:
            if (
                isinstance(field, models.ForeignKey)
                and field.name != "workspace"
                and getattr(self, field.attname)
            ):
                related = getattr(self, field.name)
                if hasattr(related, "workspace_id") and related.workspace_id != self.workspace_id:
                    raise ValidationError({field.name: "Record must belong to this workspace."})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class Record(TenantModel):
    archived = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    assigned_to = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta(TenantModel.Meta):
        abstract = True


class Organization(Record):
    name = models.CharField(max_length=180)
    website = models.URLField(blank=True)
    industry = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True, max_length=2000)

    def __str__(self):
        return self.name


class Contact(Record):
    name = models.CharField(max_length=180)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    source = models.CharField(max_length=100, blank=True)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT)

    def __str__(self):
        return self.name


class Lead(Record):
    STATUSES = [
        ("new", "New"),
        ("contacted", "Contacted"),
        ("qualified", "Qualified"),
        ("disqualified", "Disqualified"),
    ]
    name = models.CharField(max_length=180)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    source = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default="new")
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT)
    follow_up = models.DateField(null=True, blank=True)
    qualification = models.TextField(blank=True, max_length=5000)

    def __str__(self):
        return self.name


class Deal(Record):
    STAGES = [
        ("qualified", "Qualified"),
        ("proposal", "Proposal"),
        ("negotiation", "Negotiation"),
        ("won", "Won"),
        ("lost", "Lost"),
    ]
    name = models.CharField(max_length=180)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT)
    source_lead = models.OneToOneField(Lead, null=True, blank=True, on_delete=models.PROTECT)
    stage = models.CharField(max_length=20, choices=STAGES, default="qualified")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    currency = models.CharField(max_length=3)
    expected_close = models.DateField(null=True, blank=True)
    closed_on = models.DateField(null=True, blank=True)
    loss_reason = models.CharField(max_length=500, blank=True)

    class Meta(Record.Meta):
        constraints = Record.Meta.constraints + [
            models.CheckConstraint(condition=Q(amount__gte=0), name="deal_nonnegative")
        ]

    def __str__(self):
        return self.name


class Activity(TenantModel):
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT)
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT)
    lead = models.ForeignKey(Lead, null=True, blank=True, on_delete=models.PROTECT)
    deal = models.ForeignKey(Deal, null=True, blank=True, on_delete=models.PROTECT)
    creator = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")

    class Meta(TenantModel.Meta):
        abstract = True
        constraints = TenantModel.Meta.constraints + [
            models.CheckConstraint(
                condition=(
                    Q(organization__isnull=False, contact__isnull=True, lead__isnull=True, deal__isnull=True)
                    | Q(
                        organization__isnull=True, contact__isnull=False, lead__isnull=True, deal__isnull=True
                    )
                    | Q(
                        organization__isnull=True, contact__isnull=True, lead__isnull=False, deal__isnull=True
                    )
                    | Q(
                        organization__isnull=True, contact__isnull=True, lead__isnull=True, deal__isnull=False
                    )
                ),
                name="%(class)s_one_parent",
            )
        ]


class Task(Activity):
    name = models.CharField(max_length=180)
    assigned_to = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    due_date = models.DateField()
    completed = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Note(Activity):
    body = models.TextField(max_length=5000)


class Attachment(Activity):
    name = models.CharField(max_length=180)
    key = models.CharField(max_length=250, unique=True)
    size = models.PositiveIntegerField()
    content_type = models.CharField(max_length=80)
    sha256 = models.CharField(max_length=64)


class AuditEvent(TenantModel):
    actor = models.ForeignKey(Account, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=60)
    entity = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=64)

    @property
    def summary(self):
        return self.action.replace(".", " ").replace("_", " ").capitalize()


class ImportRun(TenantModel):
    digest = models.CharField(max_length=64)
    kind = models.CharField(max_length=20)
    count = models.PositiveIntegerField(default=0)

    class Meta(TenantModel.Meta):
        constraints = TenantModel.Meta.constraints + [
            models.UniqueConstraint(fields=["workspace", "digest", "kind"], name="import_once")
        ]


# Bootstrap/control-plane records are not business query surfaces. All access is
# through token validation or explicit account/workspace checks in services.
class ActionToken(models.Model):
    digest = models.CharField(max_length=64, unique=True)
    purpose = models.CharField(max_length=20)
    email = models.EmailField()
    account = models.ForeignKey(Account, null=True, on_delete=models.CASCADE)
    workspace = models.ForeignKey(Workspace, null=True, on_delete=models.CASCADE)
    role = models.CharField(max_length=10, default="member")
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)


class EmailOutbox(models.Model):
    recipient = models.EmailField()
    subject = models.CharField(max_length=180)
    encrypted_body = models.TextField()
    sent_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class RateBucket(models.Model):
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()

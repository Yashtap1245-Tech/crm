import os
from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from crmapp.models import Account, Contact, Deal, Lead, Organization, Task, Workspace
from crmapp.services import audit
from crmapp.tenancy import workspace_context


class Command(BaseCommand):
    help = "Create synthetic local demo data. Requires DEBUG and DEMO_PASSWORD."

    def handle(self, *args, **options):
        password = os.getenv("DEMO_PASSWORD")
        if not settings.DEBUG or not password:
            raise CommandError("Local demo only: set DEBUG=true and DEMO_PASSWORD.")
        if Account.objects.filter(email="demo@folio.local").exists():
            self.stdout.write("Demo already exists.")
            return
        workspace = Workspace.objects.create(name="Northstar Studio", timezone="America/Los_Angeles")
        user = Account.objects.create_user(
            username="demo@folio.local",
            email="demo@folio.local",
            password=password,
            first_name="Alex Morgan",
            workspace=workspace,
            role="member",
            verified=True,
        )
        with workspace_context(workspace.pk):
            for index, (name, company, amount, stage) in enumerate(
                [
                    ("Jordan Lee", "Evergreen Works", "12500", "proposal"),
                    ("Sam Rivera", "Alpine Collective", "8400", "qualified"),
                    ("Priya Shah", "Juniper Labs", "24000", "negotiation"),
                    ("Taylor Brooks", "Fieldwork", "6200", "won"),
                ]
            ):
                org = Organization.objects.create(
                    workspace=workspace, name=company, industry="Professional services", assigned_to=user
                )
                contact = Contact.objects.create(
                    workspace=workspace,
                    name=name,
                    email=name.split()[0].lower() + "@example.com",
                    organization=org,
                    assigned_to=user,
                    source="Referral",
                )
                lead = Lead.objects.create(
                    workspace=workspace,
                    name=name,
                    email=contact.email,
                    contact=contact,
                    assigned_to=user,
                    status="qualified",
                    source="Referral",
                )
                deal = Deal.objects.create(
                    workspace=workspace,
                    name=company + " — partnership",
                    contact=contact,
                    source_lead=lead,
                    assigned_to=user,
                    stage=stage,
                    amount=amount,
                    currency="USD",
                    closed_on=timezone.localdate() if stage == "won" else None,
                )
                Task.objects.create(
                    workspace=workspace,
                    deal=deal,
                    creator=user,
                    assigned_to=user,
                    name=[
                        "Send proposal to Jordan",
                        "Check in with Sam",
                        "Review partnership terms",
                        "Schedule project kickoff",
                    ][index],
                    due_date=timezone.localdate() + timedelta(days=index - 1),
                )
                audit(user, "deal.created", deal)
        self.stdout.write(
            "Synthetic demo ready. Email: demo@folio.local. Password is the DEMO_PASSWORD you supplied."
        )

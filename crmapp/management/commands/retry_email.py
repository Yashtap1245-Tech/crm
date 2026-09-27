from django.core.management.base import BaseCommand
from crmapp.models import EmailOutbox
from crmapp.services import deliver_mail


class Command(BaseCommand):
    help = "Retry up to 50 pending transactional emails; schedule every 15 minutes."

    def handle(self, *args, **options):
        ids = list(
            EmailOutbox.objects.filter(sent_at__isnull=True, attempts__lt=8)
            .order_by("created_at")
            .values_list("pk", flat=True)[:50]
        )
        for pk in ids:
            deliver_mail(pk)
        failed = EmailOutbox.objects.filter(sent_at__isnull=True, attempts__gte=8).count()
        self.stdout.write(f"Processed {len(ids)} pending messages; {failed} exhausted retries.")
        if failed:
            from django.core.management.base import CommandError

            raise CommandError("Email delivery needs operator attention.")

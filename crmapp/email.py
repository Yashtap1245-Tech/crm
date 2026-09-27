import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class ResendBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Idempotency-Key": message.extra_headers.get("X-Idempotency-Key", ""),
                },
                json={
                    "from": message.from_email,
                    "to": message.to,
                    "subject": message.subject,
                    "text": message.body,
                },
                timeout=15,
            )
            response.raise_for_status()
            sent += 1
        return sent

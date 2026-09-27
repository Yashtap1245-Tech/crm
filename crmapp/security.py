import base64
import hashlib
import time
from datetime import timedelta
from cryptography.fernet import Fernet
import pyotp
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from .models import Account, RateBucket


def cipher():
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is required.")
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return cipher().decrypt(value.encode()).decode()


def allowed(key, limit=10, seconds=600):
    digest = hashlib.sha256(key.encode()).hexdigest()
    with transaction.atomic():
        bucket, _ = RateBucket.objects.get_or_create(
            key=digest, defaults={"expires_at": timezone.now() + timedelta(seconds=seconds)}
        )
        bucket = RateBucket.objects.select_for_update().get(pk=bucket.pk)
        if bucket.expires_at <= timezone.now():
            bucket.count, bucket.expires_at = 0, timezone.now() + timedelta(seconds=seconds)
        bucket.count += 1
        bucket.save()
        return bucket.count <= limit


def matching_totp_step(secret, code, last_step=-1):
    totp = pyotp.TOTP(secret)
    step = int(time.time()) // 30
    for offset in (0, -1, 1):
        candidate = step + offset
        if candidate > last_step and totp.verify(code, for_time=candidate * 30):
            return candidate
    return None


def verify_totp(account, code):
    with transaction.atomic():
        user = Account.objects.select_for_update().get(pk=account.pk)
        if not user.totp_secret:
            return False
        step = matching_totp_step(decrypt(user.totp_secret), code, user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            user.save(update_fields=["totp_last_step"])
            return True
    return False

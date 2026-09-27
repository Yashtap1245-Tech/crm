from django.contrib.auth.backends import ModelBackend
from .models import Account


class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        try:
            user = Account.objects.get(email__iexact=(username or "").strip())
        except Account.DoesNotExist:
            Account().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user

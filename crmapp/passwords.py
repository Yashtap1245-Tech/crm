from django.conf import settings
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.views import PasswordResetView
from django.http import HttpResponse
from django.template.loader import render_to_string
from urllib.parse import urlparse
from .account_views import throttle
from .services import queue_mail


class QueuedResetForm(PasswordResetForm):
    def send_mail(
        self,
        subject_template_name,
        email_template_name,
        context,
        from_email,
        to_email,
        html_email_template_name=None,
    ):
        queue_mail(
            to_email,
            "".join(render_to_string(subject_template_name, context).splitlines()),
            render_to_string(email_template_name, context),
        )


class ResetView(PasswordResetView):
    template_name = "registration/password_form.html"
    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"
    form_class = QueuedResetForm

    def post(self, request, *args, **kwargs):
        if not throttle(request, "password-reset", 5):
            return HttpResponse("Too many attempts. Try again later.", status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        url = urlparse(settings.PUBLIC_URL)
        form.save(
            domain_override=url.netloc,
            use_https=url.scheme == "https",
            token_generator=self.token_generator,
            from_email=settings.DEFAULT_FROM_EMAIL,
            email_template_name=self.email_template_name,
            subject_template_name=self.subject_template_name,
            request=self.request,
        )
        from django.http import HttpResponseRedirect

        return HttpResponseRedirect(self.get_success_url())

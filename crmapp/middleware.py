from zoneinfo import ZoneInfo
from django.contrib.auth import logout
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from .tenancy import workspace_context


class WorkspaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.workspace = None
        user = request.user
        if request.path == "/logout/":
            return self.get_response(request)
        if not user.is_authenticated:
            return self.get_response(request)
        if request.session.get("session_version", user.session_version) != user.session_version:
            logout(request)
            return redirect("login")
        exempt = request.path.startswith(("/logout/", "/mfa/", "/verify/", "/resend/", "/health/"))
        if not user.verified and not exempt:
            return redirect("resend")
        if not user.workspace_id:
            return HttpResponseForbidden("This account has no workspace.")
        request.workspace = user.workspace
        if user.verified and not request.workspace.active:
            return HttpResponseForbidden("This workspace is suspended. Contact support.")
        if not exempt:
            if user.can_manage and not user.totp_secret:
                return redirect("mfa_setup")
            if user.totp_secret and request.session.get("mfa_user") != user.pk:
                return redirect("mfa_verify")
        with workspace_context(user.workspace_id), timezone.override(ZoneInfo(request.workspace.timezone)):
            return self.get_response(request)

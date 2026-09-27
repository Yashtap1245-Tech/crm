import pyotp
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .forms import CodeForm, CompanySignupForm, InviteForm, LoginForm, SignupForm, WorkspaceForm
from .models import Account, ActionToken, Workspace
from .security import allowed, decrypt, encrypt, matching_totp_step, verify_totp
from .services import audit, company_capacity_lock, find_token, queue_mail, send_verification, token_for


def throttle(request, action, limit=10):
    return allowed(f"{action}:{request.META.get('REMOTE_ADDR', 'unknown')}", limit)


def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = CompanySignupForm(request.POST or None)
    if request.method == "POST":
        if not throttle(request, "signup", 5):
            return HttpResponse("Too many attempts. Try again later.", status=429)
        if form.is_valid():
            try:
                with transaction.atomic():
                    company_capacity_lock()
                    workspace = Workspace.objects.create(name=form.cleaned_data["company"], active=False)
                    user = Account.objects.create_user(
                        username=form.cleaned_data["email"],
                        email=form.cleaned_data["email"],
                        password=form.cleaned_data["password"],
                        first_name=form.cleaned_data["name"],
                        workspace=workspace,
                        role="owner",
                    )
                    send_verification(user)
                login(request, user, backend="crmapp.auth.EmailBackend")
                request.session["session_version"] = user.session_version
                return redirect("resend")
            except (ValidationError, IntegrityError):
                form.add_error(
                    None, "Unable to register: this email is already used or pilot capacity has been reached."
                )
    return render(
        request,
        "auth.html",
        {
            "form": form,
            "title": "A clearer view of your customers.",
            "subtitle": "Create a private workspace for your team.",
            "button": "Create your workspace",
            "signup": True,
        },
    )


def sign_in(request):
    form = LoginForm(request.POST or None)
    if request.method == "POST":
        if not throttle(request, "login", 10):
            return HttpResponse("Too many attempts. Try again later.", status=429)
        if form.is_valid():
            user = authenticate(
                request, username=form.cleaned_data["email"], password=form.cleaned_data["password"]
            )
            if user:
                login(request, user)
                request.session["session_version"] = user.session_version
                return redirect("dashboard")
            form.add_error(None, "Email or password is incorrect.")
    return render(
        request,
        "auth.html",
        {
            "form": form,
            "title": "Welcome back.",
            "subtitle": "Your customers, conversations, and next steps.",
            "button": "Sign in",
        },
    )


@require_POST
def sign_out(request):
    logout(request)
    return redirect("login")


@login_required
def resend(request):
    if request.user.verified:
        return redirect("dashboard")
    if request.method == "POST":
        if not throttle(request, "resend", 3):
            return HttpResponse("Please wait before requesting another email.", status=429)
        send_verification(request.user)
        messages.success(request, "Verification email queued. Check your inbox.")
    return render(
        request,
        "message.html",
        {
            "title": "Check your email",
            "text": "Use the verification link to activate your workspace. Local development emails are written to the server console.",
            "post_label": "Resend verification email",
        },
    )


def verify(request, raw):
    if request.method == "POST":
        with transaction.atomic():
            token = find_token(raw, "verify")
            if not token:
                return render(
                    request,
                    "message.html",
                    {"title": "Link unavailable", "text": "This link has expired or was already used."},
                    status=400,
                )
            try:
                company_capacity_lock()
            except ValidationError as exc:
                return render(
                    request,
                    "message.html",
                    {"title": "Pilot at capacity", "text": exc.messages[0]},
                    status=409,
                )
            user = Account.objects.select_for_update().get(pk=token.account_id)
            if user.verified or not user.is_active:
                return render(
                    request,
                    "message.html",
                    {"title": "Link unavailable", "text": "This account cannot be activated with this link."},
                    status=400,
                )
            user.verified = True
            user.save(update_fields=["verified"])
            Workspace.objects.filter(pk=user.workspace_id).update(active=True)
            ActionToken.objects.filter(account=user, purpose="verify", used=False).update(used=True)
        messages.success(request, "Email verified. Sign in to finish setup.")
        return redirect("login")
    return render(
        request,
        "message.html",
        {
            "title": "Verify your email",
            "text": "Confirm this address to activate your company workspace.",
            "post_label": "Verify email",
        },
    )


@login_required
def mfa_setup(request):
    if not request.user.verified:
        return redirect("resend")
    if request.user.totp_secret:
        return redirect("mfa_verify")
    secret = (
        decrypt(request.session["pending_totp"])
        if request.session.get("pending_totp")
        else pyotp.random_base32()
    )
    request.session["pending_totp"] = encrypt(secret)
    form = CodeForm(request.POST or None)
    if request.method == "POST":
        if not throttle(request, "mfa", 10):
            return HttpResponse("Too many attempts.", status=429)
        step = matching_totp_step(secret, form.cleaned_data["code"]) if form.is_valid() else None
        if step is not None:
            with transaction.atomic():
                user = Account.objects.select_for_update().get(pk=request.user.pk)
                if user.totp_secret:
                    return redirect("mfa_verify")
                user.totp_secret = encrypt(secret)
                user.totp_last_step = step
                user.save(update_fields=["totp_secret", "totp_last_step"])
            request.session["mfa_user"] = request.user.pk
            request.session.pop("pending_totp", None)
            messages.success(request, "Authenticator enabled. Keep your authenticator backup secure.")
            return redirect("onboarding")
        form.add_error(None, "Code not accepted. Use this page's setup key, enable automatic date and time on your phone, and try a fresh six-digit code.")
    response = render(
        request,
        "form.html",
        {
            "form": form,
            "title": "Protect your account",
            "subtitle": "Add this setup key to an authenticator app, then enter the generated code. Keep a secure authenticator backup.",
            "secret": secret,
            "button": "Enable authenticator",
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
def mfa_verify(request):
    if not request.user.totp_secret:
        return redirect("mfa_setup")
    form = CodeForm(request.POST or None)
    if request.method == "POST":
        if not throttle(request, "mfa", 10):
            return HttpResponse("Too many attempts.", status=429)
        if form.is_valid() and verify_totp(request.user, form.cleaned_data["code"]):
            request.session["mfa_user"] = request.user.pk
            return redirect("dashboard")
        form.add_error(None, "Code invalid or already used. Wait for a new code.")
    return render(request, "form.html", {"form": form, "title": "Verify it’s you", "button": "Verify"})


@login_required
def onboarding(request):
    from .models import Contact, Lead

    form = WorkspaceForm(request.POST or None, instance=request.workspace)
    if request.method == "POST":
        if not request.user.can_manage:
            return HttpResponseForbidden()
        if form.is_valid():
            form.save()
            audit(request.user, "workspace.updated", request.workspace)
            messages.success(request, "Workspace settings saved.")
            return redirect("onboarding")
    return render(
        request,
        "onboarding.html",
        {
            "form": form,
            "has_contacts": Contact.objects.exists(),
            "has_leads": Lead.objects.exists(),
            "has_team": Account.objects.filter(workspace=request.workspace).count() > 1,
            "title": "Make yourself at home",
        },
    )


@login_required
def team(request):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    form = InviteForm(request.POST or None)
    if request.user.role != "owner":
        form.fields["role"].choices = [("member", "Member")]
    if request.method == "POST":
        if not throttle(request, "invite", 10):
            return HttpResponse("Too many invitations.", status=429)
        if form.is_valid():
            with transaction.atomic():
                Workspace.objects.select_for_update().get(pk=request.workspace.pk)
                total = Account.objects.filter(workspace=request.workspace, is_active=True).count()
                pending = ActionToken.objects.filter(
                    workspace=request.workspace, purpose="invite", used=False, expires_at__gt=timezone.now()
                ).count()
                if total + pending >= settings.WORKSPACE_USER_LIMIT:
                    form.add_error(
                        None, "Workspace seat limit reached. Revoke old invitations to free seats."
                    )
                elif Account.objects.filter(email__iexact=form.cleaned_data["email"]).exists():
                    form.add_error(
                        "email", "Use a separate email address for an account in another workspace."
                    )
                else:
                    raw = token_for(
                        "invite",
                        form.cleaned_data["email"],
                        workspace=request.workspace,
                        role=form.cleaned_data["role"],
                    )
                    queue_mail(
                        form.cleaned_data["email"],
                        f"Join {request.workspace.name}",
                        f"You have been invited to {request.workspace.name}.\n{settings.PUBLIC_URL}/invite/{raw}/\nThis invitation expires in 72 hours.",
                    )
                    audit(request.user, "member.invited", request.workspace)
                    messages.success(request, "Invitation queued.")
                    return redirect("team")
    return render(
        request,
        "team.html",
        {
            "form": form,
            "members": Account.objects.filter(workspace=request.workspace).order_by("email"),
            "invitations": ActionToken.objects.filter(
                workspace=request.workspace, purpose="invite", used=False, expires_at__gt=timezone.now()
            ),
            "title": "Your team",
        },
    )


def accept_invite(request, raw):
    if request.user.is_authenticated:
        return render(
            request,
            "message.html",
            {
                "title": "Use a separate account",
                "text": "Sign out before accepting an invitation. Each account belongs to one workspace.",
            },
            status=409,
        )
    with transaction.atomic():
        token = find_token(raw, "invite")
        if not token or not token.workspace.active:
            return render(
                request,
                "message.html",
                {"title": "Invitation unavailable", "text": "Ask your administrator for a new invitation."},
                status=400,
            )
        form = SignupForm(request.POST or None, initial={"email": token.email})
        form.fields["email"].disabled = True
        if request.method == "POST":
            if not throttle(request, "accept", 10):
                return HttpResponse("Too many attempts.", status=429)
            if form.is_valid():
                Workspace.objects.select_for_update().get(pk=token.workspace_id)
                if (
                    Account.objects.filter(workspace=token.workspace, is_active=True).count()
                    >= settings.WORKSPACE_USER_LIMIT
                ):
                    form.add_error(None, "Workspace seat limit reached.")
                else:
                    user = Account.objects.create_user(
                        username=token.email,
                        email=token.email,
                        password=form.cleaned_data["password"],
                        first_name=form.cleaned_data["name"],
                        workspace=token.workspace,
                        role=token.role,
                        verified=True,
                    )
                    token.used = True
                    token.save(update_fields=["used"])
                    login(request, user, backend="crmapp.auth.EmailBackend")
                    request.session["session_version"] = user.session_version
                    return redirect("dashboard")
    return render(
        request,
        "auth.html",
        {"form": form, "title": f"Join {token.workspace.name}", "button": "Accept invitation"},
    )


@login_required
@require_POST
def member_action(request, pk):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    with transaction.atomic():
        Workspace.objects.select_for_update().get(pk=request.workspace.pk)
        target = get_object_or_404(Account.objects.select_for_update(), pk=pk, workspace=request.workspace)
        action = request.POST.get("action")
        if action == "transfer":
            if (
                request.user.role != "owner"
                or not target.is_active
                or target.pk == request.user.pk
                or not request.user.check_password(request.POST.get("password", ""))
            ):
                return HttpResponseForbidden()
            request.user.role = "admin"
            request.user.save(update_fields=["role"])
            target.role = "owner"
        elif (
            target.role == "owner"
            or target.pk == request.user.pk
            or (target.role == "admin" and request.user.role != "owner")
        ):
            return HttpResponseForbidden()
        elif action == "deactivate":
            target.is_active = False
        elif action in ("admin", "member") and request.user.role == "owner":
            target.role = action
        else:
            return HttpResponseForbidden()
        target.session_version += 1
        target.save()
        audit(request.user, f"member.{action}", target)
    messages.success(request, "Team updated.")
    return redirect("team")


@login_required
@require_POST
def revoke_invite(request, pk):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    token = get_object_or_404(ActionToken, pk=pk, workspace=request.workspace, purpose="invite")
    if token.role == "admin" and request.user.role != "owner":
        return HttpResponseForbidden()
    token.used = True
    token.save(update_fields=["used"])
    return redirect("team")

from django.urls import path, reverse_lazy
from django.contrib.auth import views as auth_views
from crmapp import views, account_views as accounts, files
from crmapp.passwords import ResetView

urlpatterns = [
    path("deals/board/", views.deal_board, name="deal_board"),
    path("tasks/<uuid:pk>/edit/", views.task_edit, name="task_edit"),
    path("", views.dashboard, name="dashboard"),
    path("health/", views.health, name="health"),
    path("signup/", accounts.signup, name="signup"),
    path("login/", accounts.sign_in, name="login"),
    path("logout/", accounts.sign_out, name="logout"),
    path("resend/", accounts.resend, name="resend"),
    path("verify/<str:raw>/", accounts.verify, name="verify"),
    path("invite/<str:raw>/", accounts.accept_invite, name="accept_invite"),
    path("mfa/setup/", accounts.mfa_setup, name="mfa_setup"),
    path("mfa/verify/", accounts.mfa_verify, name="mfa_verify"),
    path("password/reset/", ResetView.as_view(), name="password_reset"),
    path(
        "password/sent/",
        auth_views.PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "password/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="registration/password_form.html",
            success_url=reverse_lazy("password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password/complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="registration/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
    path("onboarding/", accounts.onboarding, name="onboarding"),
    path("team/", accounts.team, name="team"),
    path("team/<int:pk>/", accounts.member_action, name="member_action"),
    path("team/invite/<int:pk>/revoke/", accounts.revoke_invite, name="revoke_invite"),
    path("tasks/", views.tasks, name="tasks"),
    path("tasks/<uuid:pk>/toggle/", views.complete_task, name="complete_task"),
    path("audit/", views.audit_log, name="audit"),
    path("leads/<uuid:pk>/convert/", views.convert, name="convert"),
    path("files/<uuid:pk>/", files.download, name="download"),
    path("records/<str:kind>/", views.record_list, name="record_list"),
    path("records/<str:kind>/new/", views.record_edit, name="record_create"),
    path("records/<str:kind>/import/", views.import_csv, name="import_csv"),
    path("records/<str:kind>/export/", views.export_csv, name="export_csv"),
    path("records/<str:kind>/<uuid:pk>/", views.record_detail, name="record_detail"),
    path("records/<str:kind>/<uuid:pk>/edit/", views.record_edit, name="record_edit"),
    path("records/<str:kind>/<uuid:pk>/archive/", views.archive, name="archive"),
    path("records/<str:kind>/<uuid:pk>/activity/", views.activity, name="activity"),
    path("records/<str:kind>/<uuid:pk>/upload/", files.upload, name="upload"),
]

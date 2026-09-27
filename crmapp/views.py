import csv
import hashlib
import io
from datetime import date
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .forms import ImportForm, RECORDS, TaskForm
from .models import Account, Attachment, AuditEvent, Contact, Deal, ImportRun, Lead, Note, Task, Workspace
from .services import audit, convert_lead, ensure_record_capacity


def spec(kind):
    if kind not in RECORDS:
        raise Http404
    return RECORDS[kind]


def parent_filter(obj):
    return {obj._meta.model_name: obj}


def filtered(request, qs):
    if request.GET.get("assigned_to", "").isdigit():
        qs = qs.filter(assigned_to_id=request.GET["assigned_to"])
    return qs


@login_required
def deal_board(request):
    qs = filtered(request, Deal.objects.filter(archived=False)).select_related("contact", "assigned_to")
    columns = []
    for stage, label in Deal.STAGES:
        items = qs.filter(stage=stage).order_by("-updated_at")
        columns.append({"label": label, "key": stage, "count": items.count(), "deals": items[:30]})
    return render(request, "board.html", {"title": "Deal pipeline", "columns": columns})


@login_required
def task_edit(request, pk):
    task = get_object_or_404(Task, pk=pk)
    form = TaskForm(request.POST or None, instance=task, workspace=request.workspace)
    if request.method == "POST" and form.is_valid():
        task = form.save()
        audit(request.user, "task.updated", task)
        return redirect("tasks")
    return render(request, "form.html", {"title": "Edit follow-up", "form": form, "button": "Save task"})


@login_required
def dashboard(request):
    deals = filtered(request, Deal.objects.filter(archived=False))
    leads = filtered(request, Lead.objects.filter(archived=False))
    tasks = filtered(request, Task.objects.filter(completed=False))
    won = deals.filter(stage="won")
    for param, lookup in [("from", "closed_on__gte"), ("to", "closed_on__lte")]:
        try:
            if request.GET.get(param):
                won = won.filter(**{lookup: date.fromisoformat(request.GET[param])})
        except ValueError:
            messages.warning(request, "Use valid ISO dates for report filters.")
    open_deals = deals.exclude(stage__in=["won", "lost"])
    stages = [
        {
            "label": label,
            "key": key,
            "count": open_deals.filter(stage=key).count(),
            "amount": open_deals.filter(stage=key).aggregate(value=Sum("amount"))["value"] or 0,
        }
        for key, label in Deal.STAGES[:3]
    ]
    return render(
        request,
        "dashboard.html",
        {
            "title": "Overview",
            "lead_count": leads.exclude(status="disqualified").count(),
            "open_value": open_deals.aggregate(value=Sum("amount"))["value"] or 0,
            "won_value": won.aggregate(value=Sum("amount"))["value"] or 0,
            "overdue": tasks.filter(due_date__lt=timezone.localdate()).count(),
            "tasks": tasks.select_related("assigned_to").order_by("due_date")[:6],
            "stages": stages,
            "recent": AuditEvent.objects.order_by("-created_at")[:8],
            "team": Account.objects.filter(workspace=request.workspace, is_active=True),
            "has_contacts": Contact.objects.exists(),
        },
    )


@login_required
def record_list(request, kind):
    model, _, label = spec(kind)
    qs = model.objects.filter(archived=request.GET.get("archived") == "1")
    query = request.GET.get("q", "").strip()[:100]
    if query:
        condition = Q(name__icontains=query)
        if kind in ("contacts", "leads"):
            condition |= Q(email__icontains=query) | Q(phone__icontains=query)
        qs = qs.filter(condition)
    qs = filtered(request, qs)
    state_field = "stage" if kind == "deals" else "status" if kind == "leads" else None
    choices = Deal.STAGES if kind == "deals" else Lead.STATUSES if kind == "leads" else []
    if state_field and request.GET.get("status"):
        qs = qs.filter(**{state_field: request.GET["status"]})
    page = Paginator(qs.select_related("assigned_to").order_by("-created_at"), 25).get_page(
        request.GET.get("page")
    )
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "list.html",
        {
            "title": label,
            "kind": kind,
            "page": page,
            "query": query,
            "choices": choices,
            "params": params.urlencode(),
            "team": Account.objects.filter(workspace=request.workspace, is_active=True),
        },
    )


@login_required
def record_edit(request, kind, pk=None):
    model, form_class, label = spec(kind)
    obj = get_object_or_404(model, pk=pk) if pk else model(workspace=request.workspace)
    if obj.archived:
        return HttpResponse("Restore this record before editing.", status=409)
    if kind == "deals" and not pk:
        obj.currency = request.workspace.currency
    form = form_class(request.POST or None, instance=obj, workspace=request.workspace, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                if pk:
                    current = model.objects.select_for_update().get(pk=pk)
                    if current.version != form.cleaned_data.get("version"):
                        raise ValidationError(
                            "Someone changed this record. Reload it before saving your changes."
                        )
                    obj.version = current.version + 1
                elif kind in ("contacts", "leads"):
                    ensure_record_capacity(request.workspace)
                if kind == "deals":
                    obj.closed_on = (
                        timezone.localdate()
                        if obj.stage in ("won", "lost") and not obj.closed_on
                        else None
                        if obj.stage not in ("won", "lost")
                        else obj.closed_on
                    )
                saved = form.save()
                audit(request.user, "record.updated" if pk else "record.created", saved)
                if (
                    kind in ("contacts", "leads")
                    and saved.email
                    and model.objects.filter(email__iexact=saved.email).exclude(pk=saved.pk).exists()
                ):
                    messages.warning(
                        request, "Another record uses this email address. Review for duplicates."
                    )
            return redirect("record_detail", kind=kind, pk=saved.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "form.html",
        {
            "title": f"{'Edit' if pk else 'New'} {model._meta.verbose_name}",
            "form": form,
            "button": "Save record",
            "back": reverse("record_list", args=[kind]),
        },
    )


@login_required
def record_detail(request, kind, pk):
    model, _, _ = spec(kind)
    obj = get_object_or_404(model, pk=pk)
    fields = []
    for field in model._meta.fields:
        if field.name not in ("id", "workspace", "version", "archived", "name"):
            value = getattr(obj, f"get_{field.name}_display")() if field.choices else getattr(obj, field.name)
            fields.append((field.verbose_name, value if value is not None and value != "" else "—"))
    parents = parent_filter(obj)
    return render(
        request,
        "detail.html",
        {
            "title": obj.name,
            "obj": obj,
            "kind": kind,
            "fields": fields,
            "notes": Note.objects.filter(**parents).select_related("creator").order_by("-created_at"),
            "tasks": Task.objects.filter(**parents)
            .select_related("assigned_to")
            .order_by("completed", "due_date"),
            "files": Attachment.objects.filter(**parents),
            "task_form": TaskForm(
                workspace=request.workspace,
                initial={"assigned_to": request.user.pk, "due_date": timezone.localdate()},
            ),
        },
    )


@login_required
@require_POST
def archive(request, kind, pk):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    model, _, _ = spec(kind)
    obj = get_object_or_404(model.objects.select_for_update(), pk=pk)
    obj.archived = not obj.archived
    obj.version += 1
    obj.save()
    audit(request.user, "record.archived" if obj.archived else "record.restored", obj)
    return redirect("record_detail", kind=kind, pk=pk)


@login_required
@require_POST
def convert(request, pk):
    get_object_or_404(Lead, pk=pk, archived=False)
    try:
        deal = convert_lead(request.user, pk)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("record_detail", kind="leads", pk=pk)
    return redirect("record_detail", kind="deals", pk=deal.pk)


@login_required
@require_POST
def activity(request, kind, pk):
    model, _, _ = spec(kind)
    obj = get_object_or_404(model, pk=pk, archived=False)
    parents = parent_filter(obj)
    if request.POST.get("action") == "note":
        body = request.POST.get("body", "").strip()
        if 0 < len(body) <= 5000:
            note = Note.objects.create(
                workspace=request.workspace, creator=request.user, body=body, **parents
            )
            audit(request.user, "note.created", note)
        else:
            messages.error(request, "Notes must contain 1–5,000 characters.")
    else:
        instance = Task(workspace=request.workspace, creator=request.user, **parents)
        form = TaskForm(request.POST, instance=instance, workspace=request.workspace)
        if form.is_valid():
            task = form.save()
            audit(request.user, "task.created", task)
        else:
            messages.error(request, "Task was not saved. Enter a title, active assignee, and valid date.")
    return redirect("record_detail", kind=kind, pk=pk)


@login_required
def tasks(request):
    qs = filtered(request, Task.objects.all())
    mode = request.GET.get("show", "open")
    if mode == "completed":
        qs = qs.filter(completed=True)
    else:
        qs = qs.filter(completed=False)
        if mode == "overdue":
            qs = qs.filter(due_date__lt=timezone.localdate())
        if mode == "today":
            qs = qs.filter(due_date=timezone.localdate())
    return render(
        request,
        "tasks.html",
        {
            "title": "Tasks",
            "tasks": Paginator(qs.select_related("assigned_to").order_by("due_date"), 50).get_page(
                request.GET.get("page")
            ),
        },
    )


@login_required
@require_POST
def complete_task(request, pk):
    task = get_object_or_404(Task.objects.select_for_update(), pk=pk)
    task.completed = not task.completed
    task.save()
    audit(request.user, "task.completed" if task.completed else "task.reopened", task)
    return redirect("tasks")


@login_required
def audit_log(request):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    return render(
        request,
        "audit.html",
        {
            "title": "Audit trail",
            "events": Paginator(
                AuditEvent.objects.select_related("actor").order_by("-created_at"), 50
            ).get_page(request.GET.get("page")),
        },
    )


def safe_cell(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value


@login_required
def export_csv(request, kind):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    model, _, _ = spec(kind)
    fields = ["name"] + (
        ["email", "phone", "source"]
        if kind in ("contacts", "leads")
        else ["website", "industry", "address"]
        if kind == "organizations"
        else ["stage", "amount", "currency", "expected_close"]
    )
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{kind}.csv"'
    writer = csv.writer(response)
    writer.writerow(fields)
    for obj in model.objects.filter(archived=False).order_by("created_at"):
        writer.writerow([safe_cell(getattr(obj, field)) for field in fields])
    audit(request.user, "records.exported", request.workspace)
    return response


@login_required
def import_csv(request, kind):
    if not request.user.can_manage:
        return HttpResponseForbidden()
    if kind not in ("contacts", "organizations", "leads"):
        raise Http404
    model, form_class, _ = spec(kind)
    fields = (
        ["name", "email", "phone", "source"]
        if kind != "organizations"
        else ["name", "website", "industry", "address"]
    )
    if request.GET.get("template") == "1":
        response = HttpResponse(",".join(fields) + "\r\n", content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{kind}-template.csv"'
        return response
    form = ImportForm(request.POST or None, request.FILES or None)
    preview = []
    errors = []
    signed = None
    if request.method == "POST" and request.POST.get("confirmed"):
        try:
            data = signing.loads(request.POST["confirmed"], salt="csv-import", max_age=900)
            if (
                data["workspace"] != str(request.workspace.pk)
                or data["user"] != request.user.pk
                or data["kind"] != kind
            ):
                raise signing.BadSignature()
            with transaction.atomic():
                Workspace.objects.select_for_update().get(pk=request.workspace.pk)
                if ImportRun.objects.filter(digest=data["digest"], kind=kind).exists():
                    messages.info(request, "This file was already imported.")
                    return redirect("record_list", kind=kind)
                for row in data["rows"]:
                    if kind in ("contacts", "leads"):
                        ensure_record_capacity(request.workspace)
                    record_form = form_class(row, workspace=request.workspace, user=request.user)
                    if not record_form.is_valid():
                        raise ValidationError("Import validation changed. Upload and preview the file again.")
                    record_form.save()
                run = ImportRun.objects.create(
                    workspace=request.workspace, digest=data["digest"], kind=kind, count=len(data["rows"])
                )
                audit(request.user, "records.imported", run)
            messages.success(request, f"Imported {len(data['rows'])} records.")
            return redirect("record_list", kind=kind)
        except (signing.BadSignature, ValidationError, KeyError):
            errors.append(
                "Import expired, exceeded capacity, or was invalid. Upload it again; no rows were committed."
            )
    elif request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        if upload.size > 1024 * 1024:
            errors.append("Maximum file size is 1 MB.")
        else:
            try:
                content = upload.read()
                reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
                if not reader.fieldnames or set(reader.fieldnames) != set(fields):
                    raise ValueError("Use the provided template headers.")
                rows = []
                for number, row in enumerate(reader, start=2):
                    if len(rows) >= 500:
                        raise ValueError("Maximum 500 rows per import.")
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"Row {number}: incorrect column count.")
                    row = {key: value.strip() for key, value in row.items()}
                    row["assigned_to"] = str(request.user.pk)
                    if kind == "leads":
                        row["status"] = "new"
                    check = form_class(row, workspace=request.workspace, user=request.user)
                    if not check.is_valid():
                        errors.append(
                            f"Row {number}: "
                            + "; ".join(f"{key}: {', '.join(values)}" for key, values in check.errors.items())
                        )
                    rows.append(row)
                if not rows:
                    raise ValueError("The file contains no records.")
                preview = rows[:10]
                if not errors:
                    signed = signing.dumps(
                        {
                            "workspace": str(request.workspace.pk),
                            "user": request.user.pk,
                            "kind": kind,
                            "rows": rows,
                            "digest": hashlib.sha256(content).hexdigest(),
                        },
                        salt="csv-import",
                        compress=True,
                    )
            except (UnicodeError, csv.Error, ValueError) as exc:
                errors.append(str(exc))
    return render(
        request,
        "import.html",
        {
            "title": f"Import {kind}",
            "kind": kind,
            "form": form,
            "fields": fields,
            "preview": preview,
            "errors": errors,
            "signed": signed,
        },
    )


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})

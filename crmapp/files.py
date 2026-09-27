import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import uuid
from PIL import Image
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import FileResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST
from .forms import RECORDS
from .models import Attachment, Workspace
from .services import audit


def s3():
    import boto3

    return boto3.client("s3", endpoint_url=settings.S3_ENDPOINT_URL, region_name=settings.S3_REGION)


def write_object(key, data, content_type):
    if settings.S3_BUCKET:
        s3().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    else:
        if not settings.DEBUG:
            raise ValueError("Private object storage is not configured.")
        path = settings.MEDIA_ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@login_required
@require_POST
def upload(request, kind, pk):
    if kind not in RECORDS:
        return HttpResponse(status=404)
    obj = get_object_or_404(RECORDS[kind][0], pk=pk, archived=False)
    file = request.FILES.get("file")
    try:
        if not file or not 0 < file.size <= 5 * 1024 * 1024:
            raise ValueError("Choose a PDF, PNG, or JPEG up to 5 MB.")
        data = file.read()
        suffix = Path(file.name).suffix.lower()
        if suffix == ".pdf" and data.startswith(b"%PDF-"):
            content_type = "application/pdf"
        elif suffix in (".png", ".jpg", ".jpeg"):
            image = Image.open(io.BytesIO(data))
            if image.format not in ("JPEG", "PNG") or image.width * image.height > 25_000_000:
                raise ValueError("Unsupported image.")
            image.verify()
            content_type = "image/png" if image.format == "PNG" else "image/jpeg"
        else:
            raise ValueError("Unsupported file type.")
        # Fail closed: no unscanned file is stored or served, including in development.
        if not settings.FILE_SCAN_COMMAND:
            raise ValueError("Uploads are unavailable until the malware scanner is configured.")
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / ("upload" + suffix)
            candidate.write_bytes(data)
            result = subprocess.run(
                [settings.FILE_SCAN_COMMAND, "--no-summary", str(candidate)], capture_output=True, timeout=30
            )
            if result.returncode != 0:
                raise ValueError("File could not be cleared by the scanner.")
        Workspace.objects.select_for_update().get(pk=request.workspace.pk)
        used = Attachment.objects.aggregate(total=Sum("size"))["total"] or 0
        if used + len(data) > settings.WORKSPACE_FILE_BYTES:
            raise ValueError("Workspace storage allowance reached.")
        key = f"{request.workspace.pk}/{uuid.uuid4().hex}{suffix}"
        write_object(key, data, content_type)
        attachment = Attachment.objects.create(
            workspace=request.workspace,
            creator=request.user,
            name=Path(file.name).name[:180],
            key=key,
            size=len(data),
            content_type=content_type,
            sha256=hashlib.sha256(data).hexdigest(),
            **{obj._meta.model_name: obj},
        )
        audit(request.user, "attachment.created", attachment)
        messages.success(request, "File uploaded securely.")
    except (ValueError, OSError, subprocess.SubprocessError, Image.DecompressionBombError) as exc:
        messages.error(request, str(exc))
    return redirect("record_detail", kind=kind, pk=pk)


@login_required
def download(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk)
    if settings.S3_BUCKET:
        body = s3().get_object(Bucket=settings.S3_BUCKET, Key=attachment.key)["Body"]
    else:
        if not settings.DEBUG:
            return HttpResponseForbidden()
        body = (settings.MEDIA_ROOT / attachment.key).open("rb")
    response = FileResponse(
        body, as_attachment=True, filename=attachment.name, content_type=attachment.content_type
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    audit(request.user, "attachment.downloaded", attachment)
    return response

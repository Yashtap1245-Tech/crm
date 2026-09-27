from contextlib import contextmanager
from contextvars import ContextVar
from django.db import connection, transaction, models

current_workspace = ContextVar("workspace", default=None)


class TenantManager(models.Manager):
    def get_queryset(self):
        qs = super().get_queryset()
        workspace = current_workspace.get()
        return qs.filter(workspace_id=workspace) if workspace else qs.none()


@contextmanager
def workspace_context(workspace_id):
    """Transaction-local database context; never survives connection reuse."""
    previous = current_workspace.get()
    token = current_workspace.set(workspace_id)
    try:
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('crm.workspace_id', %s, true)", [str(workspace_id or "")]
                    )
            try:
                yield
            finally:
                if connection.vendor == "postgresql" and not connection.needs_rollback:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT set_config('crm.workspace_id', %s, true)", [str(previous or "")]
                        )
    finally:
        current_workspace.reset(token)

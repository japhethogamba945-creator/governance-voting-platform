import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """Append-only audit record.

    ``seq``/``prev_hash``/``hash`` are filled in by the chaining job (increment
    6.8); a database trigger forbids any other update and all deletes.
    ``details`` must never contain ballot contents.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT,
                                     null=True, blank=True)  # NULL = platform-level event
    at = models.DateTimeField(default=timezone.now)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              null=True, blank=True, related_name="+")
    actor_role = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.UUIDField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    seq = models.BigIntegerField(null=True, blank=True)
    prev_hash = models.BinaryField(null=True, blank=True)
    hash = models.BinaryField(null=True, blank=True)

    class Meta:
        db_table = "audit_event"
        constraints = [
            models.UniqueConstraint(fields=["organisation", "seq"],
                                    name="audit_event_org_seq_uniq"),
        ]
        indexes = [models.Index(fields=["organisation", "at"], name="audit_event_org_at_idx")]

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """Append-only audit record.

    ``seq``/``prev_hash``/``hash`` are filled in by the chaining job
    (audit/chaining.py); a database trigger forbids any other update and all
    deletes.
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
        indexes = [
            models.Index(fields=["organisation", "at"], name="audit_event_org_at_idx"),
            models.Index(fields=["id"], condition=models.Q(hash__isnull=True),
                         name="audit_event_unchained_idx"),
        ]


class AuditAnchor(models.Model):
    """A chain head recorded outside the chain: here and as a write-once file.

    Written only by the chaining job (app_audit); never changed or removed.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT,
                                     null=True, blank=True)  # NULL = platform-level chain
    up_to_seq = models.BigIntegerField()
    head_hash = models.CharField(max_length=64)
    reason = models.CharField(max_length=200)
    location = models.CharField(max_length=500, blank=True)
    anchored_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "audit_anchor"
        ordering = ["-up_to_seq"]
        constraints = [
            models.UniqueConstraint(fields=["organisation", "up_to_seq"],
                                    name="audit_anchor_org_seq_uniq"),
        ]

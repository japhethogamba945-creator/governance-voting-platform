import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from platform_core.db import TenantManager


class ParticipationRecord(models.Model):
    """Evidence for a quorum measured by registered attendance (03 §2).

    Only ``registered_attendance`` is recorded for now: online participation is
    the consumed entitlements, and paper voting is outside the MVP (BLUEPRINT G2).
    """

    class Kind(models.TextChoices):
        REGISTERED_ATTENDANCE = "registered_attendance", "Registered attendance"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="participation")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=24, choices=Kind.choices,
                            default=Kind.REGISTERED_ATTENDANCE)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "participation_record"
        ordering = ["member__full_name"]
        constraints = [
            models.UniqueConstraint(fields=["election", "member", "kind"],
                                    name="participation_record_uniq"),
            models.CheckConstraint(condition=models.Q(kind="registered_attendance"),
                                   name="participation_record_kind_valid"),
        ]

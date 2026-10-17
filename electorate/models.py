import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from platform_core.db import TenantManager


class ElectorateEntry(models.Model):
    """One member of an election's frozen electorate.

    Created when voting opens (or, with a recorded reason, while suspended).
    Never updated or deleted (database triggers).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="electorate")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="+")
    member_category = models.CharField(max_length=32)
    subscription_status = models.CharField(max_length=8)
    amendment_reason = models.CharField(max_length=1000, blank=True)
    amended_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "electorate_entry"
        constraints = [
            models.UniqueConstraint(fields=["election", "member"],
                                    name="electorate_entry_uniq"),
            models.CheckConstraint(
                condition=Q(amendment_reason="", amended_by__isnull=True)
                | (~Q(amendment_reason="") & Q(amended_by__isnull=False)),
                name="electorate_amendment_complete"),
        ]


class Entitlement(models.Model):
    """The right to cast one ballot in one election.

    UNIQUE (election, member) means at most one per member. The only status
    changes allowed are issued -> consumed (by casting, increment 6.6) and
    issued -> revoked (while suspended, with a reason).
    """

    class Status(models.TextChoices):
        ISSUED = "issued"
        CONSUMED = "consumed"
        REVOKED = "revoked"
        PAPER = "paper"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="entitlements")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ISSUED)
    consumed_on = models.DateField(null=True, blank=True)  # deliberately only a date
    idempotency_hash = models.BinaryField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=1000, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="+")

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "entitlement"
        constraints = [
            models.UniqueConstraint(fields=["election", "member"], name="entitlement_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=["issued", "consumed", "revoked", "paper"]),
                name="entitlement_status_valid"),
            models.CheckConstraint(
                condition=~Q(status="consumed")
                | Q(idempotency_hash__isnull=False, consumed_on__isnull=False),
                name="entitlement_consumed_complete"),
            models.CheckConstraint(
                condition=~Q(status="revoked")
                | (~Q(revoked_reason="") & Q(revoked_by__isnull=False)),
                name="entitlement_revoked_complete"),
        ]

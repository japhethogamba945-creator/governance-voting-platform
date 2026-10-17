import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from platform_core.db import TenantManager

from .lifecycle import STATE_LABELS, STATES


class Meeting(models.Model):
    class Kind(models.TextChoices):
        AGM = "agm", "Annual general meeting"
        SGM = "sgm", "Special general meeting"
        OTHER = "other", "Other meeting"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.AGM)
    scheduled_at = models.DateTimeField()
    location = models.CharField(max_length=200, blank=True)
    is_remote = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "meeting"
        ordering = ["-scheduled_at"]
        constraints = [
            models.UniqueConstraint(fields=["organisation", "id"], name="meeting_org_id_uniq"),
            models.CheckConstraint(condition=Q(kind__in=["agm", "sgm", "other"]),
                                   name="meeting_kind_valid"),
        ]

    def __str__(self) -> str:
        return self.title


class Election(models.Model):
    """An election and its lifecycle state.

    State changes, configuration locking and the transition history are
    enforced by database triggers (elections/migrations/0002_lifecycle.py).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="elections")
    title = models.CharField(max_length=200)
    state = models.CharField(max_length=24, default="draft",
                             choices=[(s, STATE_LABELS[s]) for s in STATES])
    seats = models.PositiveSmallIntegerField()
    opens_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    current_rule_version = models.ForeignKey("ElectionRuleVersion", on_delete=models.PROTECT,
                                             null=True, blank=True, related_name="+")
    # Set by later increments (key ceremony, snapshot, close); write-once in the DB.
    public_key = models.BinaryField(null=True, blank=True)
    public_key_fpr = models.CharField(max_length=64, blank=True)
    electorate_digest = models.CharField(max_length=64, blank=True)
    ballot_set_digest = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "election"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["organisation", "id"], name="election_org_id_uniq"),
            models.CheckConstraint(condition=Q(seats__gte=1, seats__lte=50),
                                   name="election_seats_range"),
            models.CheckConstraint(condition=Q(state__in=list(STATES)),
                                   name="election_state_valid"),
            models.CheckConstraint(
                condition=Q(opens_at__isnull=True) | Q(closes_at__isnull=True)
                | Q(closes_at__gt=F("opens_at")),
                name="election_closes_after_opens"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def state_label(self) -> str:
        return STATE_LABELS[self.state]


class ElectionRuleVersion(models.Model):
    """An immutable-once-approved snapshot of an election's rules.

    ``rules_sha256`` is computed by the database from ``rules::text`` so anyone
    can re-verify it with SQL alone.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey(Election, on_delete=models.PROTECT,
                                 related_name="rule_versions")
    version = models.PositiveIntegerField()
    rules = models.JSONField()
    rules_sha256 = models.CharField(max_length=64, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "election_rule_version"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["election", "version"],
                                    name="rule_version_number_uniq"),
            models.UniqueConstraint(fields=["organisation", "id"],
                                    name="rule_version_org_id_uniq"),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True) | ~Q(approved_by=F("created_by")),
                name="rule_version_separation_of_duties"),
            models.CheckConstraint(
                condition=Q(approved_by__isnull=True, approved_at__isnull=True)
                | Q(approved_by__isnull=False, approved_at__isnull=False),
                name="rule_version_approval_complete"),
        ]


class LifecycleTransition(models.Model):
    """Written only by a database trigger when ``election.state`` changes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="transitions")
    from_state = models.CharField(max_length=24)
    to_state = models.CharField(max_length=24)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              null=True, blank=True, related_name="+")  # NULL = system
    reason = models.TextField(blank=True, default="")
    at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "lifecycle_transition"
        ordering = ["at"]

"""Counts, recounts, tie resolutions and certification (docs/design/03-database-design.md §2).

Tallies, their lines and tie resolutions are immutable once written; a
certification can only be published or superseded. Database triggers enforce
this and the preconditions of each step (tally/migrations/0002_guards.py).
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from platform_core.db import TenantManager

KINDS = [("original", "Original count"), ("recount", "Recount")]


class CountRequest(models.Model):
    """A count or recount started by the Returning Officer, waiting for two keyholders."""

    class Status(models.TextChoices):
        OPEN = "open", "Waiting for keyholders"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="count_requests")
    kind = models.CharField(max_length=8, choices=KINDS)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name="+")
    requested_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    tally = models.ForeignKey("Tally", on_delete=models.PROTECT, null=True, blank=True,
                              related_name="+")

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "count_request"
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(fields=["election"], condition=Q(status="open"),
                                    name="count_request_one_open"),
            models.UniqueConstraint(fields=["organisation", "id"],
                                    name="count_request_org_id_uniq"),
            models.CheckConstraint(condition=Q(kind="original") | ~Q(reason=""),
                                   name="count_request_recount_reason"),
            models.CheckConstraint(condition=Q(status__in=["open", "completed", "cancelled"]),
                                   name="count_request_status_valid"),
        ]


class KeyContribution(models.Model):
    """The first keyholder's staged pieces, sealed to the other holders.

    Deleted when the count completes or is cancelled, or after it expires
    (DECISIONS I6.7.1).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    count_request = models.ForeignKey(CountRequest, on_delete=models.CASCADE,
                                      related_name="contributions")
    share_index = models.PositiveSmallIntegerField()
    contributed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                       related_name="+")
    sealed_pieces = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "key_contribution"
        constraints = [
            models.UniqueConstraint(fields=["count_request", "share_index"],
                                    name="key_contribution_one_per_share"),
        ]


class Tally(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="tallies")
    version = models.PositiveIntegerField()
    kind = models.CharField(max_length=8, choices=KINDS)
    reason = models.TextField(blank=True, default="")
    ballot_set_digest = models.CharField(max_length=64)
    ballots_total = models.PositiveIntegerField()
    ballots_valid = models.PositiveIntegerField()
    ballots_invalid = models.PositiveIntegerField()
    ballots_blank = models.PositiveIntegerField()
    entitlements_consumed = models.PositiveIntegerField()
    electorate_size = models.PositiveIntegerField()
    reconciliation = models.JSONField()
    reconciliation_ok = models.BooleanField()
    has_ties = models.BooleanField()
    run_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                               related_name="+")
    run_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "tally"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["election", "version"], name="tally_version_uniq"),
            models.UniqueConstraint(fields=["organisation", "id"], name="tally_org_id_uniq"),
            models.UniqueConstraint(fields=["election", "id"], name="tally_election_id_uniq"),
            models.CheckConstraint(condition=Q(kind="original", version=1)
                                   | Q(kind="recount", version__gt=1),
                                   name="tally_kind_matches_version"),
            models.CheckConstraint(condition=Q(kind="original") | ~Q(reason=""),
                                   name="tally_recount_reason"),
            models.CheckConstraint(
                condition=Q(ballots_total=F("ballots_valid") + F("ballots_invalid")),
                name="tally_ballots_add_up"),
            models.CheckConstraint(condition=Q(ballots_blank__lte=F("ballots_valid")),
                                   name="tally_blank_within_valid"),
        ]


class TallyLine(models.Model):
    class Outcome(models.TextChoices):
        ELECTED = "elected", "Elected"
        NOT_ELECTED = "not_elected", "Not elected"
        TIED = "tied_pending", "Tied (awaiting the tie procedure)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    tally = models.ForeignKey(Tally, on_delete=models.PROTECT, related_name="lines")
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="+")
    candidate = models.ForeignKey("nominations.Candidate", on_delete=models.PROTECT,
                                  related_name="+")
    votes = models.PositiveIntegerField()
    rank = models.PositiveIntegerField()
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    tie_group = models.PositiveSmallIntegerField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "tally_line"
        ordering = ["rank", "candidate__ballot_order"]
        constraints = [
            models.UniqueConstraint(fields=["tally", "candidate"], name="tally_line_uniq"),
            models.CheckConstraint(condition=Q(outcome__in=["elected", "not_elected",
                                                            "tied_pending"]),
                                   name="tally_line_outcome_valid"),
            models.CheckConstraint(condition=Q(outcome="tied_pending", tie_group__isnull=False)
                                   | Q(tie_group__isnull=True)
                                   & ~Q(outcome="tied_pending"),
                                   name="tally_line_tie_group_iff_tied"),
        ]


class TieResolution(models.Model):
    """The recorded outcome of the approved tie procedure for one tie group."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    tally = models.ForeignKey(Tally, on_delete=models.PROTECT, related_name="tie_resolutions")
    tie_group = models.PositiveSmallIntegerField()
    procedure = models.CharField(max_length=40)  # copied from the approved rules
    procedure_description = models.TextField()
    elected = models.JSONField()  # candidate ids taking the seats in the tie
    not_elected = models.JSONField()
    evidence = models.TextField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name="+")
    recorded_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "tie_resolution"
        constraints = [
            models.UniqueConstraint(fields=["tally", "tie_group"], name="tie_resolution_uniq"),
        ]


class Certification(models.Model):
    """A certified result: the exact canonical document and its SHA-256.

    ``content_sha256`` is computed by the database from ``document`` so anyone
    can re-check it with SQL or by hashing the downloaded document.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="certifications")
    tally = models.ForeignKey(Tally, on_delete=models.PROTECT, related_name="+")
    certified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     related_name="+")
    statement = models.TextField()
    document = models.TextField()
    content_sha256 = models.CharField(max_length=64, blank=True)
    certified_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                     null=True, blank=True, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)
    supersede_reason = models.TextField(blank=True, default="")

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "certification"
        ordering = ["-certified_at"]
        constraints = [
            models.UniqueConstraint(fields=["election"], condition=Q(superseded_at__isnull=True),
                                    name="certification_one_active"),
            models.CheckConstraint(
                condition=Q(published_at__isnull=True, published_by__isnull=True)
                | Q(published_at__isnull=False, published_by__isnull=False),
                name="certification_publication_complete"),
            models.CheckConstraint(
                condition=Q(superseded_at__isnull=True, supersede_reason="")
                | Q(superseded_at__isnull=False) & ~Q(supersede_reason=""),
                name="certification_supersede_reason"),
        ]

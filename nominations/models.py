import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from platform_core.db import TenantManager


class Nomination(models.Model):
    """A candidacy for one election.

    Submitted by the nominee themselves, or by another member (the proposer) on
    the nominee's behalf. Everyone named who did not submit it must confirm.
    The database only accepts submissions while nominations are open and
    decisions while the election is in review.
    """

    class Status(models.TextChoices):
        SUBMITTED = "submitted"
        WITHDRAWN = "withdrawn"
        APPROVED = "approved"
        REJECTED = "rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="nominations")
    nominee = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="+")
    proposer = models.ForeignKey("members.Member", on_delete=models.PROTECT, null=True,
                                 blank=True, related_name="+")
    seconder = models.ForeignKey("members.Member", on_delete=models.PROTECT, null=True,
                                 blank=True, related_name="+")
    submitted_by = models.ForeignKey("members.Member", on_delete=models.PROTECT,
                                     related_name="+")
    statement = models.TextField(max_length=4000, blank=True)
    nominee_consented_at = models.DateTimeField(null=True, blank=True)
    proposer_confirmed_at = models.DateTimeField(null=True, blank=True)
    seconder_confirmed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)
    status_reason = models.CharField(max_length=1000, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "nomination"
        ordering = ["created_at"]
        constraints = [
            # One live nomination per member per election; withdrawn ones may be resubmitted.
            models.UniqueConstraint(fields=["election", "nominee"],
                                    condition=~Q(status="withdrawn"),
                                    name="nomination_one_live_per_nominee"),
            models.UniqueConstraint(fields=["organisation", "id"],
                                    name="nomination_org_id_uniq"),
            models.CheckConstraint(
                condition=Q(status__in=["submitted", "withdrawn", "approved", "rejected"]),
                name="nomination_status_valid"),
            models.CheckConstraint(
                condition=~Q(status="rejected") | ~Q(status_reason=""),
                name="nomination_rejection_has_reason"),
            models.CheckConstraint(
                condition=Q(status__in=["submitted", "withdrawn"])
                | Q(decided_by__isnull=False, decided_at__isnull=False),
                name="nomination_decision_recorded"),
            models.CheckConstraint(
                condition=(Q(proposer__isnull=True) | ~Q(proposer=F("nominee")))
                & (Q(seconder__isnull=True) | ~Q(seconder=F("nominee")))
                & (Q(proposer__isnull=True) | Q(seconder__isnull=True)
                   | ~Q(proposer=F("seconder"))),
                name="nomination_distinct_people"),
            models.CheckConstraint(
                condition=Q(submitted_by=F("nominee")) | Q(submitted_by=F("proposer")),
                name="nomination_submitted_by_nominee_or_proposer"),
        ]

    @property
    def outstanding_confirmations(self) -> list[str]:
        missing = []
        if self.nominee_consented_at is None:
            missing.append("nominee")
        if self.proposer_id and self.proposer_confirmed_at is None:
            missing.append("proposer")
        if self.seconder_id and self.seconder_confirmed_at is None:
            missing.append("seconder")
        return missing


class NominationDocument(models.Model):
    """A supporting file. Stored privately under a random name; served only as a download."""

    class Scan(models.TextChoices):
        NOT_SCANNED = "not_scanned", "Not scanned"
        CLEAN = "clean", "Clean"
        INFECTED = "infected", "Infected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    nomination = models.ForeignKey(Nomination, on_delete=models.PROTECT,
                                   related_name="documents")
    storage_name = models.CharField(max_length=200, unique=True)
    original_name = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64)
    mime = models.CharField(max_length=40)
    size_bytes = models.PositiveIntegerField()
    scan_status = models.CharField(max_length=12, choices=Scan.choices,
                                   default=Scan.NOT_SCANNED)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name="+")
    uploaded_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "nomination_document"
        constraints = [
            models.CheckConstraint(
                condition=Q(mime__in=["application/pdf", "image/jpeg", "image/png"]),
                name="nomination_document_mime_valid"),
            models.CheckConstraint(condition=Q(size_bytes__lte=5 * 1024 * 1024),
                                   name="nomination_document_size_limit"),
        ]


class Candidate(models.Model):
    """An approved nomination as it will appear on the ballot.

    Changes are only possible while the election is in review; the order is
    fixed when the election is submitted for approval.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    election = models.ForeignKey("elections.Election", on_delete=models.PROTECT,
                                 related_name="candidates")
    nomination = models.OneToOneField(Nomination, on_delete=models.PROTECT,
                                      related_name="candidate")
    display_name = models.CharField(max_length=200)
    statement = models.TextField(max_length=4000, blank=True)
    ballot_order = models.PositiveSmallIntegerField()

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "candidate"
        ordering = ["ballot_order"]
        constraints = [
            models.UniqueConstraint(fields=["election", "ballot_order"],
                                    name="candidate_ballot_order_uniq",
                                    deferrable=models.Deferrable.DEFERRED),
            models.UniqueConstraint(fields=["organisation", "id"], name="candidate_org_id_uniq"),
            models.UniqueConstraint(fields=["election", "id"],
                                    name="candidate_election_id_uniq"),
        ]

    def __str__(self) -> str:
        return self.display_name

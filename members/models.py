import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from platform_core.db import TenantManager

DEFAULT_CATEGORIES = ("full", "associate", "life", "honorary")
PHONE_VALIDATOR = RegexValidator(r"^\+?[0-9 ]{7,20}$",
                                 "Enter a phone number such as +254 700 000000.")
MEMBER_NUMBER_VALIDATOR = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9/._-]{0,31}$",
                                         "Use letters, digits, '/', '.', '_' or '-'.")


class Member(models.Model):
    """An entry in an organisation's membership register.

    Fields that affect election eligibility (category, subscription status,
    joined date, status) are audited with old and new values. Edits never affect
    an election that is already open: its electorate is a frozen snapshot
    (increment 6.5).
    """

    class Status(models.TextChoices):
        ACTIVE = "active"
        INACTIVE = "inactive"

    class Subscription(models.TextChoices):
        PAID = "paid", "Paid"
        UNPAID = "unpaid", "Unpaid"
        EXEMPT = "exempt", "Exempt"
        NOT_APPLICABLE = "n/a", "Not applicable"

    ELIGIBILITY_FIELDS = ("category", "subscription_status", "joined_on", "status")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    member_number = models.CharField(max_length=32, validators=[MEMBER_NUMBER_VALIDATOR])
    full_name = models.CharField(max_length=200)
    email = models.EmailField(max_length=254, blank=True)  # stored lower-case; '' = none
    phone = models.CharField(max_length=24, blank=True, validators=[PHONE_VALIDATOR])
    category = models.CharField(max_length=32)
    subscription_status = models.CharField(max_length=8, choices=Subscription.choices)
    joined_on = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivation_reason = models.CharField(max_length=500, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "member"
        ordering = ["full_name", "member_number"]
        constraints = [
            models.UniqueConstraint(fields=["organisation", "member_number"],
                                    name="member_number_uniq"),
            models.UniqueConstraint(fields=["organisation", "email"], condition=~Q(email=""),
                                    name="member_email_uniq"),
            models.UniqueConstraint(fields=["organisation", "user"],
                                    condition=Q(user__isnull=False), name="member_user_uniq"),
            models.UniqueConstraint(fields=["organisation", "id"], name="member_org_id_uniq"),
            models.CheckConstraint(condition=Q(email=Lower("email")), name="member_email_lower"),
            models.CheckConstraint(condition=Q(status__in=["active", "inactive"]),
                                   name="member_status_valid"),
            models.CheckConstraint(
                condition=Q(subscription_status__in=["paid", "unpaid", "exempt", "n/a"]),
                name="member_subscription_valid"),
            models.CheckConstraint(
                condition=Q(status="active", deactivated_at__isnull=True)
                | Q(status="inactive", deactivated_at__isnull=False),
                name="member_deactivation_consistent"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.member_number})"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE


class Invitation(models.Model):
    """A single-use, expiring invitation to set up an account for a member.

    Only the SHA-256 of the token is stored. Rows are visible outside the
    organisation's context only to a caller who presents the token (RLS).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name="invitations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    token_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "invitation"
        constraints = [
            models.UniqueConstraint(
                fields=["member"], condition=Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="invitation_one_open_per_member"),
            models.CheckConstraint(condition=Q(expires_at__gt=models.F("created_at")),
                                   name="invitation_expiry_after_creation"),
            models.CheckConstraint(condition=Q(accepted_at__isnull=True)
                                   | Q(revoked_at__isnull=True),
                                   name="invitation_not_accepted_and_revoked"),
        ]

    @property
    def is_open(self) -> bool:
        return (self.accepted_at is None and self.revoked_at is None
                and self.expires_at > timezone.now())


class ImportBatch(models.Model):
    """A previewed CSV import. Applying it re-validates against the current register."""

    class Status(models.TextChoices):
        PREVIEWED = "previewed"
        APPLIED = "applied"
        DISCARDED = "discarded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey("tenancy.Organisation", on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    file_sha256 = models.CharField(max_length=64)
    # Parsed rows as submitted; cleared once the batch is applied or discarded.
    # Abandoned previews are purged by the retention job (Phase 8 runbook).
    rows = models.JSONField()
    plan_digest = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PREVIEWED)
    applied_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "member_import_batch"
        constraints = [
            models.CheckConstraint(condition=Q(status__in=["previewed", "applied", "discarded"]),
                                   name="import_batch_status_valid"),
        ]

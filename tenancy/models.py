import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone as dj_tz

from platform_core.db import TenantManager

from .roles import Role

SLUG_PATTERN = r"^[a-z0-9-]{3,40}$"


class Organisation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active"
        SUSPENDED = "suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.CharField(max_length=40, unique=True,
                            validators=[RegexValidator(SLUG_PATTERN)])
    legal_name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default="Africa/Nairobi")
    governance_settings = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(default=dj_tz.now)

    class Meta:
        db_table = "organisation"
        constraints = [
            models.CheckConstraint(condition=Q(slug__regex=SLUG_PATTERN),
                                   name="organisation_slug_format"),
            models.CheckConstraint(condition=Q(status__in=["active", "suspended"]),
                                   name="organisation_status_valid"),
        ]

    def __str__(self) -> str:
        return self.legal_name


class OrgMembership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             related_name="memberships")
    created_at = models.DateTimeField(default=dj_tz.now)

    objects = TenantManager()
    # Not organisation-filtered in the application. RLS still applies; used for
    # "my organisations" (own-membership policy) and platform provisioning.
    all_objects = models.Manager()

    class Meta:
        db_table = "org_membership"
        constraints = [
            models.UniqueConstraint(fields=["organisation", "user"], name="org_membership_uniq"),
            models.UniqueConstraint(fields=["organisation", "id"],
                                    name="org_membership_org_id_uniq"),
        ]


class RoleAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.PROTECT)
    membership = models.ForeignKey(OrgMembership, on_delete=models.PROTECT, related_name="roles")
    role = models.CharField(max_length=32, choices=Role.choices)
    # NULL = organisation-wide; election-scoped roles arrive with elections (6.3).
    election_id = models.UUIDField(null=True, blank=True)
    # NULL = granted during platform provisioning (see the audit event).
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                   null=True, blank=True, related_name="+")
    valid_from = models.DateTimeField(default=dj_tz.now)
    valid_to = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "role_assignment"
        constraints = [
            models.CheckConstraint(condition=Q(role__in=[r.value for r in Role]),
                                   name="role_assignment_role_valid"),
            models.CheckConstraint(condition=Q(valid_to__isnull=True)
                                   | Q(valid_to__gt=models.F("valid_from")),
                                   name="role_assignment_validity"),
        ]

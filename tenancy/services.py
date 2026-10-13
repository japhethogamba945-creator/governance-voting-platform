from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.services import record as audit
from platform_core.db import db_context

from .models import Organisation, OrgMembership, RoleAssignment
from .roles import GRANTABLE_ROLES, Role


def provision_organisation(*, slug: str, legal_name: str, admin_email: str,
                           admin_name: str = "", using: str = "platform"):
    """Create an organisation and its first administrator (Platform Owner action).

    Runs on the ``platform`` connection (role ``app_platform``). The admin
    account is created as ``invited`` with no usable password; the invitation
    flow (increment 6.2) lets them set a password and enrol a second factor.
    """
    User = get_user_model()
    with transaction.atomic(using=using):
        org = Organisation(slug=slug, legal_name=legal_name)
        org.full_clean()
        org.save(using=using)
        user = User.objects.db_manager(using).filter(
            email=User.objects.normalize_email(admin_email)).first()
        if user is None:
            user = User.objects.db_manager(using).create_user(
                admin_email, full_name=admin_name, status=User.Status.INVITED)
        with db_context(org_id=org.id, using=using):
            membership = OrgMembership.objects.db_manager(using).create(
                organisation=org, user=user)
            RoleAssignment.objects.db_manager(using).create(
                organisation=org, membership=membership, role=Role.ORG_ADMIN)
            audit("organisation.provisioned", organisation_id=org.id, using=using,
                  object_type="organisation", object_id=org.id,
                  details={"slug": slug, "first_admin_user_id": str(user.id)})
    return org, user


class RoleError(ValueError):
    pass


def grant_role(membership: OrgMembership, role: str, *, actor, actor_membership) -> RoleAssignment:
    """Grant an official role to an existing member of the organisation."""
    if role not in {r.value for r in GRANTABLE_ROLES}:
        raise RoleError("That role cannot be granted here.")
    if membership.id == actor_membership.id:
        raise RoleError("You cannot change your own roles.")
    now = timezone.now()
    if RoleAssignment.objects.filter(
        membership=membership, role=role, election_id__isnull=True
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now)).exists():
        raise RoleError("This person already has that role.")
    assignment = RoleAssignment.objects.create(
        organisation_id=membership.organisation_id, membership=membership, role=role,
        granted_by=actor)
    audit("role.granted", actor=actor, organisation_id=membership.organisation_id,
          object_type="role_assignment", object_id=assignment.id,
          details={"role": role, "user_id": str(membership.user_id)})
    return assignment


def revoke_role(assignment: RoleAssignment, *, actor, actor_membership) -> None:
    now = timezone.now()
    if assignment.role not in {r.value for r in GRANTABLE_ROLES}:
        raise RoleError("That role is managed elsewhere (membership register or nominations).")
    if assignment.membership_id == actor_membership.id:
        raise RoleError("You cannot change your own roles.")
    if assignment.valid_to is not None and assignment.valid_to <= now:
        raise RoleError("That role has already ended.")
    if assignment.role == Role.ORG_ADMIN:
        # Lock every active admin assignment (including this one). A concurrent
        # revocation waits here, then re-evaluates the filter after this commits,
        # so two revocations cannot leave the organisation without an administrator.
        active_admins = list(
            RoleAssignment.objects.select_for_update()
            .filter(role=Role.ORG_ADMIN, election_id__isnull=True, valid_to__isnull=True)
            .values_list("id", flat=True))
        if assignment.pk not in active_admins:
            raise RoleError("That role has already ended.")
        if len(active_admins) < 2:
            raise RoleError("An organisation must keep at least one administrator.")
    assignment.valid_to = now
    assignment.save(update_fields=["valid_to"])
    audit("role.revoked", actor=actor, organisation_id=assignment.organisation_id,
          object_type="role_assignment", object_id=assignment.id,
          details={"role": assignment.role, "user_id": str(assignment.membership.user_id)})

"""Membership register operations. Callers run inside the organisation's context."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from audit.services import record as audit
from platform_core.db import current_org_id, db_context, lookup_scope
from tenancy.models import OrgMembership, RoleAssignment
from tenancy.roles import Role

from . import emails
from .models import Invitation, Member


class MemberError(ValueError):
    """A register operation that is not allowed in the member's current state."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _snapshot(member: Member) -> dict:
    return {f.attname: getattr(member, f.attname) for f in Member._meta.concrete_fields}


def _audit_member(action: str, member: Member, actor, before: dict | None,
                  extra: dict | None = None) -> None:
    """Audit a register change.

    Eligibility-relevant fields are recorded with old and new values; other
    personal fields only by name (the audit trail is not a second register).
    """
    after = _snapshot(member)
    changed = sorted(k for k in after if before is None or before.get(k) != after[k])
    changed = [k for k in changed if k not in {"updated_at", "created_at", "id"}]
    eligibility = {
        f: [None if before is None else str(before.get(f)), str(after[f])]
        for f in Member.ELIGIBILITY_FIELDS if f in changed
    }
    details = {"changed": changed, "eligibility": eligibility, **(extra or {})}
    audit(action, actor=actor, organisation_id=member.organisation_id,
          object_type="member", object_id=member.id, details=details)


def save_member(member: Member, *, actor, before: dict | None) -> Member:
    """Persist a created (``before is None``) or edited member and audit it."""
    if before is None:
        member.organisation_id = current_org_id()
    elif before["email"] != member.email and member.user_id is not None:
        raise MemberError("The email of a member who already has an account cannot be changed.")
    member.updated_at = timezone.now()
    member.save()
    _audit_member("member.created" if before is None else "member.updated", member, actor, before)
    return member


def sync_member_role(member: Member, *, actor=None) -> None:
    """The MEMBER role follows the register: active member => active MEMBER role."""
    if member.user_id is None:
        return
    membership, _ = OrgMembership.objects.get_or_create(
        organisation_id=member.organisation_id, user_id=member.user_id)
    now = timezone.now()
    active = RoleAssignment.objects.filter(
        membership=membership, role=Role.MEMBER, election_id__isnull=True
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
    if member.is_active and not active.exists():
        RoleAssignment.objects.create(organisation_id=member.organisation_id,
                                      membership=membership, role=Role.MEMBER, granted_by=actor)
    elif not member.is_active:
        active.update(valid_to=now)


def _revoke_open_invitations(member: Member) -> int:
    return Invitation.objects.filter(
        member=member, accepted_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())


def deactivate_member(member: Member, *, reason: str, actor) -> None:
    if not member.is_active:
        raise MemberError("This member is already inactive.")
    reason = reason.strip()
    if not reason:
        raise MemberError("A reason is required.")
    before = _snapshot(member)
    member.status = Member.Status.INACTIVE
    member.deactivated_at = timezone.now()
    member.deactivation_reason = reason[:500]
    member.updated_at = timezone.now()
    member.save()
    _revoke_open_invitations(member)
    sync_member_role(member, actor=actor)
    _audit_member("member.deactivated", member, actor, before, {"reason": reason[:500]})


def reactivate_member(member: Member, *, actor) -> None:
    if member.is_active:
        raise MemberError("This member is already active.")
    before = _snapshot(member)
    member.status = Member.Status.ACTIVE
    member.deactivated_at = None
    member.deactivation_reason = ""
    member.updated_at = timezone.now()
    member.save()
    sync_member_role(member, actor=actor)
    _audit_member("member.reactivated", member, actor, before)


def invite_member(member: Member, *, actor) -> None:
    """Send an account invitation, or link the member to an existing account.

    The administrator sees the same outcome either way, so the register does not
    reveal whether an email already has an account elsewhere on the platform.
    """
    if not member.is_active:
        raise MemberError("Only active members can be invited.")
    if not member.email:
        raise MemberError("Add an email address before sending an invitation.")
    User = get_user_model()
    with lookup_scope(email=member.email):
        user = User.objects.filter(email=member.email).first()
    if member.user_id is not None and (user is None or user.id != member.user_id):
        raise MemberError("This member is linked to a different account. Contact support.")
    if user is not None and user.status in (User.Status.LOCKED, User.Status.DISABLED):
        raise MemberError("The account for this email is locked or disabled. Contact support.")
    if user is None:
        user = User.objects.create_user(member.email, full_name=member.full_name,
                                        status=User.Status.INVITED)
    if member.user_id is None:
        member.user = user
        member.updated_at = timezone.now()
        member.save(update_fields=["user", "updated_at"])
    sync_member_role(member, actor=actor)
    _revoke_open_invitations(member)

    if user.status == User.Status.ACTIVE:
        emails.send_added_to_organisation(member)
    else:
        token = secrets.token_urlsafe(32)
        now = timezone.now()
        invitation = Invitation.objects.create(
            organisation_id=member.organisation_id, member=member, user=user,
            token_hash=hash_token(token), created_by=actor,
            created_at=now, expires_at=now + timedelta(days=settings.INVITATION_TTL_DAYS),
        )
        emails.send_invitation(member, token, invitation.expires_at)
    audit("member.invited", actor=actor, organisation_id=member.organisation_id,
          object_type="member", object_id=member.id)


def find_open_invitation(token: str) -> Invitation | None:
    """Look up an invitation by its token (outside any organisation context)."""
    if not token or len(token) > 100:
        return None
    with lookup_scope(invite_token_hash=hash_token(token)):
        invitation = (
            Invitation.all_objects.select_related("organisation", "user", "member")
            .filter(token_hash=hash_token(token)).first()
        )
    return invitation if invitation is not None and invitation.is_open else None


def accept_invitation(token: str, password: str | None = None) -> Invitation:
    """Set the invited user's password and activate the account.

    The second factor is enrolled immediately afterwards (pre-MFA flow). For an
    account that is already active (set up through another organisation) no
    password is taken and the invitation is simply marked accepted.
    """
    invitation = find_open_invitation(token)
    if invitation is None:
        raise MemberError("This invitation link is invalid or has expired.")
    user = invitation.user
    now = timezone.now()
    with lookup_scope(invite_token_hash=hash_token(token)):
        if user.status == user.Status.INVITED:
            if not password:
                raise MemberError("A password is required to set up the account.")
            user.set_password(password)
            user.status = user.Status.ACTIVE
            user.email_verified_at = now
            user.save(update_fields=["password", "status", "email_verified_at"])
        invitation.accepted_at = now
        invitation.save(update_fields=["accepted_at"])
    with db_context(org_id=invitation.organisation_id):
        audit("member.invitation_accepted", actor=user,
              organisation_id=invitation.organisation_id,
              object_type="member", object_id=invitation.member_id)
    return invitation


def apply_import(batch, *, actor) -> dict:
    """Apply a previewed import batch, all or nothing."""
    from datetime import date

    from .importing import build_plan, plan_digest, summarise

    if batch.status != batch.Status.PREVIEWED:
        raise MemberError("This import has already been applied or discarded.")
    # Lock the register so the plan cannot change between the check and the writes.
    list(Member.objects.select_for_update().values_list("id", flat=True))
    plan = build_plan(batch.organisation, batch.rows)
    if plan_digest(plan) != batch.plan_digest:
        raise MemberError("The register changed since this preview. Upload the file again.")
    counts = summarise(plan)
    if counts["error"]:
        raise MemberError("Fix the errors in the file and upload it again.")
    existing = {m.member_number: m for m in Member.objects.all()}
    for item in plan:
        if item.action not in ("create", "update"):
            continue
        data = dict(item.cleaned, joined_on=date.fromisoformat(item.cleaned["joined_on"]))
        member = existing.get(item.member_number)
        before = None if member is None else _snapshot(member)
        if member is None:
            member = Member(**data)
        else:
            for name, value in data.items():
                setattr(member, name, value)
        member.full_clean(exclude=["organisation", "user"], validate_unique=False,
                          validate_constraints=False)
        save_member(member, actor=actor, before=before)
    batch.status = batch.Status.APPLIED
    batch.applied_at = timezone.now()
    batch.rows = []  # data minimisation: the register now holds the data
    batch.save(update_fields=["status", "applied_at", "rows"])
    audit("member.import_applied", actor=actor, organisation_id=batch.organisation_id,
          object_type="member_import_batch", object_id=batch.id,
          details={"counts": counts, "file_sha256": batch.file_sha256})
    return counts

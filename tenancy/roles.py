from enum import StrEnum

from django.db import models


class Role(models.TextChoices):
    ORG_ADMIN = "org_admin", "Organisation administrator"
    RETURNING_OFFICER = "returning_officer", "Returning Officer"
    AUDITOR = "auditor", "Auditor / observer"
    CANDIDATE = "candidate", "Candidate"
    MEMBER = "member", "Member"


OFFICIAL_ROLES = frozenset({Role.ORG_ADMIN, Role.RETURNING_OFFICER, Role.AUDITOR})


class Perm(StrEnum):
    """Organisation-level permissions (docs/design/02-architecture.md §6).

    Election permissions are added with the increments that introduce them.
    """

    ORG_VIEW = "org.view"
    ORG_MANAGE_SETTINGS = "org.manage_settings"
    MEMBERS_VIEW = "members.view"
    MEMBERS_MANAGE = "members.manage"
    ROLES_GRANT = "roles.grant"
    ROLES_VIEW = "roles.view"
    AUDIT_VIEW = "audit.view"
    ELECTIONS_VIEW = "elections.view"
    ELECTIONS_EDIT = "elections.edit"  # create and configure while preparing
    MEETINGS_MANAGE = "meetings.manage"
    NOMINATIONS_SUBMIT = "nominations.submit"  # members: nominate, confirm, withdraw
    NOMINATIONS_VIEW = "nominations.view"
    NOMINATIONS_REVIEW = "nominations.review"  # approve or reject
    KEYS_HOLD = "keys.hold"  # set a keyholder passphrase; hold election key shares
    MARKED_REGISTER_VIEW = "voting.marked_register"  # who voted (never how): RO, Auditor
    TALLY_RUN = "tally.run"  # start counts and recounts, resolve ties, certify
    TALLY_VIEW = "tally.view"  # counts before publication
    RESULTS_PUBLISH = "results.publish"  # publish a certified result to members


ROLE_PERMISSIONS: dict[Role, frozenset[Perm]] = {
    Role.ORG_ADMIN: frozenset({
        Perm.ORG_VIEW, Perm.ORG_MANAGE_SETTINGS, Perm.MEMBERS_VIEW, Perm.MEMBERS_MANAGE,
        Perm.ROLES_GRANT, Perm.ROLES_VIEW, Perm.AUDIT_VIEW,
        Perm.ELECTIONS_VIEW, Perm.ELECTIONS_EDIT, Perm.MEETINGS_MANAGE, Perm.NOMINATIONS_VIEW,
        Perm.KEYS_HOLD, Perm.RESULTS_PUBLISH,
    }),
    Role.RETURNING_OFFICER: frozenset({Perm.ORG_VIEW, Perm.MEMBERS_VIEW, Perm.ROLES_VIEW,
                                       Perm.AUDIT_VIEW, Perm.ELECTIONS_VIEW,
                                       Perm.ELECTIONS_EDIT, Perm.MEETINGS_MANAGE,
                                       Perm.NOMINATIONS_VIEW, Perm.NOMINATIONS_REVIEW,
                                       Perm.KEYS_HOLD, Perm.MARKED_REGISTER_VIEW,
                                       Perm.TALLY_RUN, Perm.TALLY_VIEW,
                                       Perm.RESULTS_PUBLISH}),
    Role.AUDITOR: frozenset({Perm.ORG_VIEW, Perm.ROLES_VIEW, Perm.AUDIT_VIEW,
                             Perm.ELECTIONS_VIEW, Perm.NOMINATIONS_VIEW, Perm.KEYS_HOLD,
                             Perm.MARKED_REGISTER_VIEW, Perm.TALLY_VIEW}),
    Role.CANDIDATE: frozenset({Perm.ORG_VIEW}),
    Role.MEMBER: frozenset({Perm.ORG_VIEW, Perm.NOMINATIONS_SUBMIT}),
}

# Roles an organisation administrator may grant through the roles page.
# MEMBER follows the membership register; CANDIDATE follows nominations (6.4).
GRANTABLE_ROLES = (Role.ORG_ADMIN, Role.RETURNING_OFFICER, Role.AUDITOR)


def permissions_for(roles) -> frozenset[Perm]:
    """A membership without any active role grants nothing (and is treated as no access)."""
    perms: set[Perm] = set()
    for role in roles:
        perms |= ROLE_PERMISSIONS.get(Role(role), frozenset())
    return frozenset(perms)

"""Election configuration and lifecycle operations. Callers run in the organisation context."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Max
from django.utils import timezone

from audit.services import record as audit
from members.importing import categories_for
from platform_core.db import current_org_id, db_context

from . import lifecycle, rules
from .models import Election, ElectionRuleVersion, Meeting

MIN_VOTING_PERIOD = timedelta(hours=1)
CONFIG_FIELDS = ("title", "meeting_id", "seats", "opens_at", "closes_at")


class ElectionError(ValueError):
    """An operation that is not allowed in the election's current state."""


# Checks and effects other apps run inside a transition, after its own
# preconditions and before the state changes, in the same transaction:
# ``hook(election, target, actor, context)``. ``actor`` is None for system
# transitions; ``context`` carries request data (e.g. chosen keyholders) and
# lets hooks hand results back (e.g. the one-time backup code). A hook raises
# ElectionError to block the transition. Registered from AppConfig.ready().
TRANSITION_HOOKS: list = []


def register_transition_hook(hook):
    TRANSITION_HOOKS.append(hook)
    return hook


def _check_times(opens_at, closes_at) -> None:
    if opens_at and closes_at and closes_at - opens_at < MIN_VOTING_PERIOD:
        raise ElectionError("Voting must stay open for at least one hour.")


def create_meeting(meeting: Meeting, *, actor) -> Meeting:
    meeting.organisation_id = current_org_id()
    meeting.created_by = actor
    meeting.save()
    audit("meeting.created", actor=actor, organisation_id=meeting.organisation_id,
          object_type="meeting", object_id=meeting.id)
    return meeting


def save_meeting(meeting: Meeting, *, actor) -> Meeting:
    meeting.save()
    audit("meeting.updated", actor=actor, organisation_id=meeting.organisation_id,
          object_type="meeting", object_id=meeting.id)
    return meeting


def create_election(election: Election, *, actor) -> Election:
    _check_times(election.opens_at, election.closes_at)
    election.organisation_id = current_org_id()
    election.created_by = actor
    election.state = lifecycle.DRAFT
    election.save()
    audit("election.created", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"seats": election.seats})
    return election


def update_election(election: Election, *, actor, before: dict) -> Election:
    if before["state"] not in lifecycle.EDITABLE_STATES:
        raise ElectionError("The election's configuration is locked in its current state.")
    _check_times(election.opens_at, election.closes_at)
    changed = [f for f in CONFIG_FIELDS if before[f] != getattr(election, f)]
    if not changed:
        return election
    if "seats" in changed and election.current_rule_version_id:
        current = election.current_rule_version.rules
        if current["max_selections"] > election.seats:
            raise ElectionError("Reduce the maximum number of choices in the rules first.")
    Election.objects.filter(pk=election.pk).update(**{f: getattr(election, f) for f in changed})
    audit("election.updated", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"changed": changed, **({"seats": [before["seats"], election.seats]}
                                          if "seats" in changed else {})})
    return election


def snapshot(election: Election) -> dict:
    return {"state": election.state, **{f: getattr(election, f) for f in CONFIG_FIELDS}}


def save_rules(election: Election, new_rules: dict, *, actor) -> ElectionRuleVersion:
    """Store a new rule version and make it current (only while preparing)."""
    if election.state not in lifecycle.EDITABLE_STATES:
        raise ElectionError("Rules can only change while the election is being prepared.")
    problems = rules.validate_rules(new_rules, election.seats,
                                    categories_for(election.organisation))
    if problems:
        raise ElectionError(" ".join(problems))
    Election.objects.select_for_update().filter(pk=election.pk).first()
    latest = ElectionRuleVersion.objects.filter(election=election).aggregate(n=Max("version"))["n"]
    version = ElectionRuleVersion.objects.create(
        organisation_id=election.organisation_id, election=election,
        version=(latest or 0) + 1, rules=new_rules, created_by=actor)
    version.refresh_from_db(fields=["rules_sha256"])  # computed by the database
    Election.objects.filter(pk=election.pk).update(current_rule_version=version)
    election.current_rule_version = version
    audit("election.rules_saved", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"version": version.version, "rules_sha256": version.rules_sha256})
    return version


def _set_reason(reason: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.transition_reason', %s, true)", [reason])


def transition(election: Election, target: str, *, actor, roles, reason: str = "",
               context: dict | None = None) -> Election:
    """Move an election to ``target`` if the actor's roles and the state allow it."""
    reason = (reason or "").strip()[:1000]
    context = {} if context is None else context
    current = (Election.objects.select_for_update(of=("self",))
               .select_related("current_rule_version").get(pk=election.pk))
    spec = lifecycle.TRANSITIONS.get((current.state, target))
    if spec is None:
        raise ElectionError("That change is not possible from the election's current state.")
    if not spec.available:
        raise ElectionError(f"“{spec.label}” is not available yet. {spec.note}".strip())
    if not (spec.roles & set(roles)):
        raise PermissionDenied
    if spec.reason_required and not reason:
        raise ElectionError("A reason is required.")

    version = current.current_rule_version
    if target == lifecycle.READY:
        if version is None:
            raise ElectionError("Set the election rules before submitting for approval.")
        if current.opens_at is None or current.closes_at is None:
            raise ElectionError("Set the voting opening and closing times first.")
        problems = rules.validate_rules(version.rules, current.seats,
                                        categories_for(current.organisation))
        if problems:
            raise ElectionError(" ".join(problems))
    if target == lifecycle.SCHEDULED:
        if version.created_by_id == actor.id:
            raise ElectionError("The rules must be approved by someone other than the person "
                                "who wrote them.")
        if current.opens_at <= timezone.now():
            raise ElectionError("The opening time has passed. Return the election to draft and "
                                "set new times.")
        version.approved_by = actor
        version.approved_at = timezone.now()
        version.save(update_fields=["approved_by", "approved_at"])
        audit("election.rules_approved", actor=actor, organisation_id=current.organisation_id,
              object_type="election", object_id=current.id,
              details={"version": version.version, "rules_sha256": version.rules_sha256})

    for hook in TRANSITION_HOOKS:
        hook(current, target, actor, context)
    return _apply_transition(current, target, actor=actor, reason=reason)


def system_transition(election: Election, target: str, *, reason: str = "",
                      context: dict | None = None) -> Election:
    """A transition performed by the scheduler (no person), e.g. opening at ``opens_at``."""
    context = {} if context is None else context
    current = (Election.objects.select_for_update(of=("self",))
               .select_related("current_rule_version").get(pk=election.pk))
    spec = lifecycle.TRANSITIONS.get((current.state, target))
    if spec is None or spec.roles != lifecycle.SYSTEM or not spec.available:
        raise ElectionError(f"{current.state} -> {target} is not an automatic transition.")
    if target == lifecycle.OPEN:
        if current.opens_at is None or current.opens_at > timezone.now():
            raise ElectionError("The opening time has not been reached.")
        if current.public_key is None:
            raise ElectionError("The election has no public key (no key ceremony).")
    for hook in TRANSITION_HOOKS:
        hook(current, target, None, context)
    return _apply_transition(current, target, actor=None, reason=reason)


def _apply_transition(current: Election, target: str, *, actor, reason: str) -> Election:
    # The history row is written by a trigger from app.user_id and
    # app.transition_reason: set both explicitly so attribution never depends
    # on the caller's context.
    with db_context(user_id=actor.id if actor else None, org_id=current.organisation_id):
        _set_reason(reason)
        try:
            updated = (Election.objects.filter(pk=current.pk, state=current.state)
                       .update(state=target))
        finally:
            _set_reason("")
    if updated != 1:  # pragma: no cover - guarded by the row lock above
        raise ElectionError("The election changed while you were working. Please try again.")
    audit("election.transition", actor=actor, organisation_id=current.organisation_id,
          object_type="election", object_id=current.id,
          details={"from": current.state, "to": target, **({"reason": reason} if reason else {})})
    current.state = target
    return current

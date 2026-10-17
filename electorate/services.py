"""The frozen electorate and voting entitlements (created when voting opens)."""

import hashlib
import logging

from django.db import transaction
from django.utils import timezone

from audit.services import record as audit
from elections import lifecycle
from elections.eligibility import evaluate
from elections.models import Election
from elections.services import ElectionError, register_transition_hook, system_transition
from members.models import Member
from platform_core.db import db_context, scheduler_scope

from .models import ElectorateEntry, Entitlement

log = logging.getLogger(__name__)


def electorate_digest(entries) -> str:
    """SHA-256 over the sorted member ids and the attributes that made them eligible."""
    lines = sorted(f"{e.member_id}|{e.member_category}|{e.subscription_status}" for e in entries)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def build_electorate(election: Election) -> int:
    """Freeze the electorate and issue one entitlement per eligible member.

    Runs inside the opening transition, while the election is still
    'scheduled' (the database refuses snapshot rows at any other time, except
    reasoned amendments while suspended).
    """
    rules = election.current_rule_version.rules
    tz = election.organisation.timezone
    eligible = [m for m in Member.objects.filter(status=Member.Status.ACTIVE)
                if evaluate(m, rules, tz).eligible]
    if not eligible:
        raise ElectionError("No members are eligible to vote under the approved rules.")
    entries = ElectorateEntry.objects.bulk_create(
        ElectorateEntry(organisation_id=election.organisation_id, election=election, member=m,
                        member_category=m.category, subscription_status=m.subscription_status)
        for m in eligible)
    Entitlement.objects.bulk_create(
        Entitlement(organisation_id=election.organisation_id, election=election, member=m)
        for m in eligible)
    digest = electorate_digest(entries)
    Election.objects.filter(pk=election.pk).update(electorate_digest=digest)
    election.electorate_digest = digest
    audit("election.electorate_frozen", organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"size": len(entries), "digest": digest})
    return len(entries)


@register_transition_hook
def lifecycle_hook(election, target: str, actor, context: dict) -> None:
    if target == lifecycle.OPEN and election.state == lifecycle.SCHEDULED:
        context["electorate_size"] = build_electorate(election)


def _run_due(state: str, time_field: str, target: str, label, now) -> list[tuple[str, str]]:
    """Apply a system transition to every election in ``state`` whose ``time_field`` passed.

    Each election runs in its own transaction and organisation context, so one
    failure does not stop the others; failures are audited and the election is
    left as it was for the Returning Officer to act on.
    """
    with transaction.atomic(), scheduler_scope():
        due = list(Election.all_objects.filter(state=state, **{f"{time_field}__lte": now})
                   .values_list("id", "organisation_id"))
    results = []
    for election_id, org_id in due:
        try:
            with transaction.atomic(), db_context(org_id=org_id):
                election = (Election.objects.select_related("current_rule_version",
                                                            "organisation").get(pk=election_id))
                context: dict = {}
                system_transition(election, target, context=context)
            results.append((str(election_id), label(context)))
        except ElectionError as exc:
            with transaction.atomic(), db_context(org_id=org_id):
                audit(f"election.{target}_failed", organisation_id=org_id,
                      object_type="election", object_id=election_id,
                      details={"error": str(exc)[:500]})
            log.warning("Could not move election %s to %s: %s", election_id, target, exc)
            results.append((str(election_id), f"failed: {exc}"))
    return results


def close_due_elections(now=None) -> list[tuple[str, str]]:
    """Close every open election whose closing time has passed (seals the ballot box)."""
    return _run_due(lifecycle.OPEN, "closes_at", lifecycle.CLOSED,
                    lambda c: f"closed ({c['ballots']} ballots)", now or timezone.now())


def open_due_elections(now=None) -> list[tuple[str, str]]:
    """Open every scheduled election whose opening time has passed.

    Each election is opened in its own transaction and organisation context, so
    one failure (for example an empty electorate) does not stop the others; the
    failure is audited and the election stays scheduled for the Returning
    Officer to act on. Returns (election_id, outcome) pairs.
    """
    return _run_due(lifecycle.SCHEDULED, "opens_at", lifecycle.OPEN,
                    lambda c: f"opened ({c['electorate_size']} voters)", now or timezone.now())

"""Casting a secret ballot and closing the ballot box.

One person, one vote (docs/design/BLUEPRINT.md §4.2):

* The whole cast runs in one transaction as the ``app_voting`` role: lock the
  election row FOR SHARE (closing takes FOR UPDATE, so it waits for in-flight
  casts and blocks new ones), lock the member's entitlement FOR UPDATE, validate,
  seal, insert the ballot and consume the entitlement. Either all of it commits
  or none of it does.
* A retry with the same idempotency key after a successful cast is answered
  "accepted" (the truthful replay); any other attempt finds the entitlement
  consumed. UNIQUE (election, member) on entitlements and the database triggers
  are the backstop if this code is ever wrong.
* The ballot row has no voter reference and no timestamp. Nothing here logs,
  audits or stores the selections.
"""

import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from django.core import signing
from django.db import connection
from django.utils import timezone

from audit.services import record as audit
from elections import lifecycle
from elections.services import register_transition_hook

from . import encoding
from .models import Ballot

_SIGNER_SALT = "voting.idempotency"


class BallotRejected(ValueError):
    """The ballot was not accepted (and nothing was recorded)."""


@dataclass(frozen=True)
class CastResult:
    status: str  # "accepted" | "replayed" | "already_voted"
    receipt_code: str = ""  # only when the server generated it (no-JavaScript fallback)


@contextmanager
def as_db_role(role: str):
    """Switch the current transaction to ``role`` (SET LOCAL ROLE), then switch back."""
    if not connection.in_atomic_block:
        raise RuntimeError("as_db_role() must be used inside transaction.atomic()")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        previous = cursor.fetchone()[0]
        cursor.execute("SELECT set_config('role', %s, true)", [role])
    try:
        yield
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('role', %s, true)", [previous])


def issue_idempotency_key(entitlement_id: uuid.UUID) -> str:
    """A signed key, bound to the voter's entitlement, embedded in the confirmation form."""
    return signing.Signer(salt=_SIGNER_SALT).sign(f"{entitlement_id}:{uuid.uuid4().hex}")


def _check_idempotency_key(key: str, entitlement_id: uuid.UUID) -> bytes:
    try:
        value = signing.Signer(salt=_SIGNER_SALT).unsign(key)
    except signing.BadSignature as exc:
        raise BallotRejected("This ballot form is not valid. Please start again.") from exc
    if value.split(":", 1)[0] != str(entitlement_id):
        raise BallotRejected("This ballot form is not valid. Please start again.")
    return hashlib.sha256(key.encode()).digest()


def validate_selections(selections: list[str], candidate_ids: set[uuid.UUID],
                        rules: dict) -> list[uuid.UUID]:
    try:
        chosen = [uuid.UUID(str(s)) for s in selections]
    except ValueError as exc:
        raise BallotRejected("The ballot contains an unknown choice.") from exc
    if len(set(chosen)) != len(chosen):
        raise BallotRejected("A candidate was chosen more than once.")
    if not set(chosen) <= candidate_ids:
        raise BallotRejected("The ballot contains an unknown choice.")
    if len(chosen) > rules["max_selections"]:
        raise BallotRejected(f"You can choose at most {rules['max_selections']} "
                             f"candidate{'s' if rules['max_selections'] != 1 else ''}.")
    if not chosen and not rules["blank_ballot_allowed"]:
        raise BallotRejected("Choose at least one candidate.")
    if chosen and len(chosen) < rules["min_selections"]:
        raise BallotRejected(f"Choose at least {rules['min_selections']} candidates.")
    if chosen and len(chosen) < rules["max_selections"] and not rules["partial_ballots_allowed"]:
        raise BallotRejected(f"Choose exactly {rules['max_selections']} candidates.")
    return chosen


def _fetch_one(sql: str, params: list):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def cast(*, election_id: uuid.UUID, member_id: uuid.UUID, selections: list[str],
         idempotency_key: str, receipt_commitment: bytes | None, actor) -> CastResult:
    """Cast a ballot. Must run inside the request transaction and organisation context.

    The caller must not report success unless the surrounding transaction commits;
    with RequestTransactionMiddleware the response is only sent after it does.
    """
    with as_db_role("app_voting"):
        election = _fetch_one(
            "SELECT e.state, e.public_key, e.organisation_id, v.rules::text, "
            "coalesce(now() >= e.opens_at AND now() < e.closes_at, false) AS in_window "
            "FROM election e "
            "LEFT JOIN election_rule_version v ON v.id = e.current_rule_version_id "
            "WHERE e.id = %s FOR SHARE OF e", [election_id])
        if election is None:
            raise BallotRejected("This election does not exist.")
        state, public_key, organisation_id, rules_json, in_window = election
        if state != lifecycle.OPEN or not in_window or rules_json is None:
            raise BallotRejected("Voting is not open for this election. Your ballot was not "
                                 "recorded.")
        entitlement = _fetch_one(
            "SELECT id, status, idempotency_hash FROM entitlement "
            "WHERE election_id = %s AND member_id = %s FOR UPDATE", [election_id, member_id])
        if entitlement is None:
            raise BallotRejected("You are not on the voter list for this election.")
        entitlement_id, status, stored_hash = entitlement
        key_hash = _check_idempotency_key(idempotency_key, entitlement_id)
        if status == "consumed":
            replay = stored_hash is not None and bytes(stored_hash) == key_hash
            return CastResult("replayed" if replay else "already_voted")
        if status != "issued":
            raise BallotRejected("Your entitlement to vote in this election is not active. "
                                 "Contact the Returning Officer.")

        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM candidate WHERE election_id = %s", [election_id])
            candidate_ids = {row[0] for row in cursor.fetchall()}
        chosen = validate_selections(selections, candidate_ids, json.loads(rules_json))

        receipt_code = ""
        if receipt_commitment is None:  # no JavaScript: the server makes the receipt code
            receipt_code = encoding.new_receipt_code()
            receipt_commitment = encoding.receipt_commitment(election_id, receipt_code)
        if len(receipt_commitment) != 32:
            raise BallotRejected("This ballot form is not valid. Please start again.")

        ciphertext = encoding.seal(bytes(public_key), encoding.encode(election_id, chosen))
        Ballot.objects.create(organisation_id=organisation_id, election_id=election_id,
                              ciphertext=ciphertext, receipt_commitment=receipt_commitment)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE entitlement SET status = 'consumed', consumed_on = current_date, "
                "idempotency_hash = %s WHERE id = %s", [key_hash, entitlement_id])
        # Records *that* this member voted (not secret from the Returning Officer and
        # Auditor), at minute precision, and never how.
        audit("ballot.accepted", actor=actor, organisation_id=organisation_id,
              object_type="election", object_id=election_id,
              at=timezone.now().replace(second=0, microsecond=0))
    return CastResult("accepted", receipt_code=receipt_code)


def ballot_summary(election_id) -> tuple[int, str]:
    """(count, set digest) of the election's ballots, without reading their contents.

    Uses the SECURITY DEFINER function ballot_set_summary(): the web role cannot
    read the ballot table.
    """
    row = _fetch_one("SELECT ballot_count, digest FROM ballot_set_summary(%s)", [election_id])
    return row[0], row[1]


@register_transition_hook
def lifecycle_hook(election, target: str, actor, context: dict) -> None:
    """Seal the ballot box when voting closes: record its digest and reconcile."""
    if not (target == lifecycle.CLOSED and election.state == lifecycle.OPEN):
        return
    from elections.models import Election
    from electorate.models import Entitlement

    count, digest = ballot_summary(election.id)
    consumed = Entitlement.objects.filter(election=election, status="consumed").count()
    Election.objects.filter(pk=election.pk).update(ballot_set_digest=digest)
    election.ballot_set_digest = digest
    context["ballots"] = count
    audit("election.ballot_box_sealed", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"ballots": count, "entitlements_consumed": consumed, "digest": digest,
                   "reconciled": count == consumed})
    # A mismatch is recorded, not hidden; it blocks certification (increment 6.7).
    context["reconciled"] = count == consumed

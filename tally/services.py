"""Counting, recounts, ties, certification and publication.

Counting (docs/design/02-architecture.md §5.2, DECISIONS I6.7.1):

1. The Returning Officer starts a count (or a recount, with a reason).
2. A first keyholder, in their own session, provides their passphrase (or an
   official enters the offline backup code). Their share is opened and the one
   piece they share with each other holder is sealed to that holder; nothing the
   server stores is enough to rebuild the key.
3. A second, different person does the same. Their share plus the piece staged
   for them rebuild the election key, in memory, and the count runs at once in
   the same transaction: every ballot is decrypted and checked, totals are
   computed, reconciled and written as an immutable tally, and the staged
   pieces are deleted. The key is never stored.

Individual ballots are never stored, logged or returned in plaintext; only the
totals are.
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from audit.models import AuditEvent
from audit.services import record as audit
from elections import lifecycle, rules
from elections.models import Election
from elections.services import ElectionError, flow_transition, register_transition_hook
from electorate.models import Entitlement
from keys import services as key_services
from nominations.models import Candidate
from platform_core.db import db_context, scheduler_scope
from quorum import services as quorum_services
from voting import encoding
from voting.services import as_db_role

from . import counting
from .models import Certification, CountRequest, KeyContribution, Tally, TallyLine, TieResolution

log = logging.getLogger(__name__)

CONTRIBUTION_TTL = timedelta(minutes=30)
DOCUMENT_SCHEMA = "election-result/v1"
RECOUNT_STATES = (lifecycle.COUNTED, lifecycle.AWAITING, lifecycle.DISPUTED)
STATEMENT = (
    "Certified by the Returning Officer. This document records the count of the ballots "
    "held in the sealed electronic ballot box of this election, counted under the approved "
    "election rules identified in this document. The number of ballots matched the number "
    "of members recorded as having voted, and every ballot was valid."
)


class TallyError(ValueError):
    """A counting or certification step that is not possible now."""


@dataclass(frozen=True)
class ContributionResult:
    status: str  # "staged" | "counted"
    tally: Tally | None = None


# --- helpers ---------------------------------------------------------------------------

def _lock_election(election_id) -> Election:
    return (Election.objects.select_for_update(of=("self",))
            .select_related("current_rule_version", "organisation").get(pk=election_id))


def latest_tally(election) -> Tally | None:
    return Tally.objects.filter(election=election).order_by("-version").first()


def open_request(election) -> CountRequest | None:
    return CountRequest.objects.filter(election=election,
                                       status=CountRequest.Status.OPEN).first()


def active_certification(election) -> Certification | None:
    return Certification.objects.filter(election=election, superseded_at__isnull=True).first()


def tie_resolution(tally: Tally) -> TieResolution | None:
    return TieResolution.objects.filter(tally=tally).first()


def final_outcomes(tally: Tally) -> dict[uuid.UUID, str]:
    lines = [counting.Line(line.candidate_id, line.votes, line.rank, line.outcome,
                           line.tie_group) for line in tally.lines.all()]
    resolution = tie_resolution(tally) if tally.has_ties else None
    elected = {uuid.UUID(c) for c in resolution.elected} if resolution else set()
    return counting.resolved_outcomes(lines, elected)


def certification_blockers(election, tally: Tally | None) -> list[str]:
    """Why the latest count cannot be certified yet (empty when it can)."""
    if tally is None:
        return ["The election has not been counted."]
    problems = []
    if not tally.reconciliation_ok:
        problems.append("Reconciliation failed: the count cannot be certified. Follow the "
                        "incident procedure.")
    if tally.has_ties and tie_resolution(tally) is None:
        problems.append("A tie for the last seat(s) must be resolved first.")
    return problems


# --- starting and cancelling ------------------------------------------------------------

def start_count(election, *, actor, reason: str = "") -> CountRequest:
    """Start the original count (closed) or a recount (counted, awaiting or disputed)."""
    reason = (reason or "").strip()[:1000]
    election = _lock_election(election.pk)
    if open_request(election) is not None:
        raise TallyError("A count is already waiting for keyholders.")
    if election.state == lifecycle.CLOSED:
        kind = "original"
    elif election.state in RECOUNT_STATES:
        kind = "recount"
        if not reason:
            raise TallyError("A recount needs a reason.")
    else:
        raise TallyError("The election cannot be counted in its current state.")
    request = CountRequest.objects.create(organisation_id=election.organisation_id,
                                          election=election, kind=kind, reason=reason,
                                          requested_by=actor)
    audit("count.requested", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"kind": kind, **({"reason": reason} if reason else {})})
    return request


def cancel_count(request: CountRequest, *, actor) -> None:
    request = CountRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != CountRequest.Status.OPEN:
        raise TallyError("That count is no longer waiting.")
    deleted, _ = KeyContribution.objects.filter(count_request=request).delete()
    CountRequest.objects.filter(pk=request.pk).update(status=CountRequest.Status.CANCELLED,
                                                      closed_at=timezone.now())
    audit("count.cancelled", actor=actor, organisation_id=request.organisation_id,
          object_type="election", object_id=request.election_id,
          details={"staged_contributions_deleted": deleted})


# --- keyholder contributions ----------------------------------------------------------

def contribute(request: CountRequest, *, actor, passphrase: str | None = None,
               backup_code: str | None = None) -> ContributionResult:
    """Provide a key share to a waiting count. The second contribution runs the count."""
    request = CountRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != CountRequest.Status.OPEN:
        raise TallyError("That count is no longer waiting for keyholders.")
    election = _lock_election(request.election_id)
    expected = (lifecycle.CLOSED,) if request.kind == "original" else RECOUNT_STATES
    if election.state not in expected:
        raise TallyError("The election is no longer in a state where it can be counted.")
    KeyContribution.objects.filter(count_request=request, expires_at__lt=timezone.now()).delete()
    contribution = ({"backup_code": backup_code} if backup_code
                    else {"user": actor, "passphrase": passphrase or ""})
    staged = list(KeyContribution.objects.filter(count_request=request))
    if any(c.contributed_by_id == actor.id for c in staged):
        raise TallyError("You have already provided a share. A different person must provide "
                         "the second one.")
    index = key_services.share_index_for(election, contribution)
    if index is None:
        raise TallyError("You are not a keyholder for this election."
                         if "user" in contribution else "This election has no backup share.")
    if any(c.share_index == index for c in staged):
        raise TallyError("That share has already been provided.")
    try:
        if not staged:
            index, pieces = key_services.stage_share(election, contribution)
            KeyContribution.objects.create(
                organisation_id=request.organisation_id, count_request=request,
                share_index=index, contributed_by=actor, sealed_pieces=pieces,
                expires_at=timezone.now() + CONTRIBUTION_TTL)
            audit("count.share_provided", actor=actor, organisation_id=request.organisation_id,
                  object_type="election", object_id=election.id,
                  details={"share_index": index, "backup": "backup_code" in contribution})
            return ContributionResult("staged")
        first, own, private_key = key_services.rebuild_from_staged(
            election, contribution, {c.share_index: c.sealed_pieces for c in staged})
    except key_services.KeysError as exc:
        raise TallyError(str(exc)) from exc
    try:
        tally = _run_count(election, request, private_key, actor=actor, shares=[first, own])
    finally:
        del private_key
    return ContributionResult("counted", tally)


def _reconcile(*, digest: str, sealed_digest: str, result: counting.Count, consumed: int,
               electorate: int) -> dict:
    return {
        "ballot_box_matches_seal": digest == sealed_digest,
        "ballots_equal_voters_marked": result.ballots_total == consumed,
        "all_ballots_valid": result.ballots_invalid == 0,
        "voters_within_electorate": consumed <= electorate,
    }


def _run_count(election: Election, request: CountRequest, private_key, *, actor,
               shares: list[int]) -> Tally:
    version_rules = election.current_rule_version.rules
    candidates = list(Candidate.objects.filter(election=election).order_by("ballot_order")
                      .values_list("id", flat=True))
    consumed = Entitlement.objects.filter(election=election, status="consumed").count()
    electorate = Entitlement.objects.filter(election=election).count()
    version = (Tally.objects.filter(election=election).aggregate(v=Max("version"))["v"] or 0) + 1
    with as_db_role("app_tally"):
        with connection.cursor() as cursor:
            cursor.execute("SELECT ciphertext FROM ballot WHERE election_id = %s", [election.id])
            ciphertexts = [bytes(row[0]) for row in cursor.fetchall()]
        result = counting.count(private_key, ciphertexts, election_id=election.id,
                                candidates=candidates, rules=version_rules, seats=election.seats)
        digest = encoding.ballot_set_digest(ciphertexts)
        del ciphertexts
        checks = _reconcile(digest=digest, sealed_digest=election.ballot_set_digest,
                            result=result, consumed=consumed, electorate=electorate)
        tally = Tally.objects.create(
            organisation_id=election.organisation_id, election=election, version=version,
            kind=request.kind, reason=request.reason, ballot_set_digest=digest,
            ballots_total=result.ballots_total, ballots_valid=result.ballots_valid,
            ballots_invalid=result.ballots_invalid, ballots_blank=result.ballots_blank,
            entitlements_consumed=consumed, electorate_size=electorate,
            reconciliation=checks, reconciliation_ok=all(checks.values()),
            has_ties=result.has_ties, run_by_id=request.requested_by_id)
        TallyLine.objects.bulk_create([
            TallyLine(organisation_id=election.organisation_id, tally=tally, election=election,
                      candidate_id=line.candidate_id, votes=line.votes, rank=line.rank,
                      outcome=line.outcome, tie_group=line.tie_group)
            for line in result.lines])
        audit("tally.completed", actor=actor, organisation_id=election.organisation_id,
              object_type="election", object_id=election.id,
              details={"version": version, "kind": request.kind, "digest": digest,
                       "ballots": result.ballots_total, "invalid": result.ballots_invalid,
                       "reconciliation_ok": tally.reconciliation_ok,
                       "has_ties": tally.has_ties, "shares_used": sorted(shares)})

    if election.state == lifecycle.DISPUTED:
        superseded = Certification.objects.filter(election=election,
                                                  superseded_at__isnull=True)
        for certification in superseded:
            audit("certification.superseded", actor=request.requested_by,
                  organisation_id=election.organisation_id, object_type="election",
                  object_id=election.id,
                  details={"content_sha256": certification.content_sha256,
                           "reason": request.reason})
        superseded.update(superseded_at=timezone.now(), supersede_reason=request.reason)
    if election.state != lifecycle.COUNTED:
        flow_transition(election, lifecycle.COUNTED, actor=request.requested_by,
                        reason=request.reason)
    if tally.reconciliation_ok and not tally.has_ties:
        flow_transition(election, lifecycle.AWAITING, actor=None)
    KeyContribution.objects.filter(count_request=request).delete()
    CountRequest.objects.filter(pk=request.pk).update(
        status=CountRequest.Status.COMPLETED, closed_at=timezone.now(), tally=tally)
    if not tally.reconciliation_ok:
        log.error("Election %s: count version %s failed reconciliation", election.id, version)
    return tally


def purge_expired_contributions() -> int:
    """Delete staged contributions past their expiry, in every organisation (scheduler)."""
    with transaction.atomic(), scheduler_scope():
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM key_contribution WHERE expires_at < now() "
                           "RETURNING organisation_id, count_request_id, share_index")
            rows = cursor.fetchall()
    for org_id, request_id, share_index in rows:
        with transaction.atomic(), db_context(org_id=org_id):
            election_id = (CountRequest.objects.filter(pk=request_id)
                           .values_list("election_id", flat=True).first())
            audit("count.share_expired", organisation_id=org_id, object_type="election",
                  object_id=election_id, details={"share_index": share_index})
    return len(rows)


# --- ties ----------------------------------------------------------------------------------

def resolve_tie(election, *, actor, elected_ids: list[str], evidence: str) -> TieResolution:
    """Record the outcome of the approved tie procedure for the latest count."""
    election = _lock_election(election.pk)
    if election.state != lifecycle.COUNTED:
        raise TallyError("Ties are resolved while the election is counted.")
    tally = latest_tally(election)
    if tally is None or not tally.has_ties or tie_resolution(tally) is not None:
        raise TallyError("There is no unresolved tie in the latest count.")
    evidence = (evidence or "").strip()
    if len(evidence) < 10:
        raise TallyError("Describe how the tie procedure was carried out (who, when, witnesses).")
    lines = list(tally.lines.all())
    tied = [str(line.candidate_id) for line in lines if line.outcome == counting.TIED]
    seats = election.seats - sum(1 for line in lines if line.outcome == counting.ELECTED)
    chosen = list(dict.fromkeys(str(c) for c in elected_ids))
    if not set(chosen) <= set(tied) or len(chosen) != seats:
        raise TallyError(f"Choose exactly {seats} of the tied candidates.")
    procedure = election.current_rule_version.rules["tie_procedure"]
    resolution = TieResolution.objects.create(
        organisation_id=election.organisation_id, tally=tally,
        tie_group=counting.BOUNDARY_TIE_GROUP, procedure=procedure["type"],
        procedure_description=procedure["description"], elected=chosen,
        not_elected=[c for c in tied if c not in chosen], evidence=evidence[:4000],
        recorded_by=actor)
    audit("tally.tie_resolved", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"version": tally.version, "procedure": procedure["type"],
                   "elected": chosen})
    if tally.reconciliation_ok:
        flow_transition(election, lifecycle.AWAITING, actor=None)
    return resolution


# --- the certified result document ----------------------------------------------------------

def receipt_commitments(election) -> list[str]:
    """Sorted hex receipt commitments of a sealed ballot box (empty before closing)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT c FROM ballot_receipt_commitments(%s) AS c", [election.id])
        return [row[0] for row in cursor.fetchall()]


def receipts_digest(commitments: list[str]) -> str:
    return hashlib.sha256("\n".join(commitments).encode()).hexdigest()


def audit_head(org_id) -> dict:
    """The latest chained audit event: certifying fixes the trail up to this point."""
    head = (AuditEvent.objects.filter(organisation_id=org_id, seq__isnull=False)
            .order_by("-seq").values("seq", "hash").first())
    return ({"chain_head_seq": head["seq"], "chain_head_hash": bytes(head["hash"]).hex()}
            if head else {"chain_head_seq": 0, "chain_head_hash": ""})


def canonical(document: dict) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def result_document(election, tally: Tally, *, certified_by, certified_at) -> dict:
    version = election.current_rule_version
    outcomes = final_outcomes(tally)
    names = dict(Candidate.objects.filter(election=election)
                 .values_list("id", "display_name"))
    resolution = tie_resolution(tally) if tally.has_ties else None
    commitments = receipt_commitments(election)
    return {
        "schema": DOCUMENT_SCHEMA,
        "organisation": {"name": election.organisation.legal_name,
                         "slug": election.organisation.slug},
        "election": {
            "id": str(election.id), "title": election.title, "seats": election.seats,
            "voting_opened": election.opens_at.isoformat(),
            "voting_closed": election.closes_at.isoformat(),
            "rules_version": version.version, "rules_sha256": version.rules_sha256,
            "counting_method": version.rules["counting_method"],
            "public_key_fingerprint": election.public_key_fpr,
            "electorate_digest": election.electorate_digest,
            "ballot_set_digest": election.ballot_set_digest,
        },
        "count": {
            "tally_id": str(tally.id), "version": tally.version, "kind": tally.kind,
            "reason": tally.reason, "counted_at": tally.run_at.isoformat(),
            "electorate": tally.electorate_size,
            "voters_marked": tally.entitlements_consumed,
            "ballots": tally.ballots_total, "valid": tally.ballots_valid,
            "invalid": tally.ballots_invalid, "blank": tally.ballots_blank,
            "reconciliation": tally.reconciliation,
        },
        "results": [
            {"candidate_id": str(line.candidate_id), "name": names[line.candidate_id],
             "votes": line.votes, "rank": line.rank, "outcome": outcomes[line.candidate_id]}
            for line in tally.lines.select_related("candidate")
        ],
        "tie_resolution": None if resolution is None else {
            "procedure": rules.TIE_PROCEDURES[resolution.procedure],
            "procedure_description": resolution.procedure_description,
            "elected": [names[uuid.UUID(c)] for c in resolution.elected],
            "not_elected": [names[uuid.UUID(c)] for c in resolution.not_elected],
            "evidence": resolution.evidence,
            "recorded_at": resolution.recorded_at.isoformat(),
        },
        "quorum": quorum_services.for_tally(election, tally),
        "receipts": {"count": len(commitments), "sha256": receipts_digest(commitments)},
        "audit": audit_head(election.organisation_id),
        "statement": STATEMENT,
        "certified_by": certified_by.full_name or certified_by.email,
        "certified_at": certified_at.isoformat(),
    }


# --- certification and publication -------------------------------------------------------

def certify(election, *, actor) -> Certification:
    election = _lock_election(election.pk)
    if election.state != lifecycle.AWAITING:
        raise TallyError("Only an election awaiting certification can be certified.")
    tally = latest_tally(election)
    problems = certification_blockers(election, tally)
    if problems:
        raise TallyError(" ".join(problems))
    if election.current_rule_version.created_by_id == actor.id:
        raise TallyError("The person who wrote the election rules cannot certify the result.")
    certified_at = timezone.now()
    text = canonical(result_document(election, tally, certified_by=actor,
                                     certified_at=certified_at))
    certification = Certification.objects.create(
        organisation_id=election.organisation_id, election=election, tally=tally,
        certified_by=actor, statement=STATEMENT, document=text, certified_at=certified_at)
    certification.refresh_from_db(fields=["content_sha256"])  # computed by the database
    if certification.content_sha256 != hashlib.sha256(text.encode()).hexdigest():
        raise TallyError("The certified document's fingerprint does not match.")  # pragma: no cover
    flow_transition(election, lifecycle.CERTIFIED, actor=actor)
    audit("election.certified", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"tally_version": tally.version,
                   "content_sha256": certification.content_sha256})
    return certification


def publish(election, *, actor) -> Certification:
    election = _lock_election(election.pk)
    certification = active_certification(election)
    if election.state != lifecycle.CERTIFIED or certification is None:
        raise TallyError("Only a certified result can be published.")
    if certification.published_at is not None:
        raise TallyError("This result is already published.")
    Certification.objects.filter(pk=certification.pk).update(published_at=timezone.now(),
                                                             published_by=actor)
    certification.refresh_from_db()
    audit("results.published", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"content_sha256": certification.content_sha256})
    return certification


def published_result(election) -> Certification | None:
    """The result members may see: the current, published certification."""
    return Certification.objects.filter(election=election, superseded_at__isnull=True,
                                        published_at__isnull=False).first()


@register_transition_hook
def recertify_hook(election, target: str, actor, context: dict) -> None:
    """Re-certifying a disputed result needs the certification it confirms."""
    if target == lifecycle.CERTIFIED and election.state == lifecycle.DISPUTED:
        tally = latest_tally(election)
        certification = active_certification(election)
        if certification is None or tally is None or certification.tally_id != tally.id:
            raise ElectionError("There is no current certified result to confirm. Order a "
                                "recount instead.")

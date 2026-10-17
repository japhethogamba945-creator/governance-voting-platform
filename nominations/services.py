"""Nominations and the candidate list. Callers run inside the organisation's context."""

import hashlib
import json
import os
import secrets
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.db.models import Max, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from audit.services import record as audit
from elections import lifecycle
from elections.eligibility import evaluate
from elections.services import ElectionError, register_transition_hook
from members.models import Member
from tenancy.models import OrgMembership, RoleAssignment
from tenancy.roles import Role

from .models import Candidate, Nomination, NominationDocument

MAX_DOCUMENTS = 3
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
_MAGIC = (
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
)


class NominationError(ValueError):
    pass


def member_for(user) -> Member | None:
    """The signed-in user's own active register entry in the current organisation."""
    return Member.objects.filter(user=user, status=Member.Status.ACTIVE).first()


def _rules(election) -> dict:
    if election.current_rule_version is None:
        raise NominationError("This election has no rules yet.")
    return election.current_rule_version.rules


def _check_eligible(member: Member, election, role: str) -> None:
    result = evaluate(member, _rules(election), election.organisation.timezone)
    if not result.eligible:
        raise NominationError(f"The {role} is not eligible: {'; '.join(result.reasons)}.")


def _notify(member: Member | None, nomination: Nomination, role: str) -> None:
    """Tell someone named on a nomination that their confirmation is needed."""
    if member is None or not member.email or not settings.SITE_URL:
        return
    link = settings.SITE_URL.rstrip("/") + reverse(
        "nominations:mine", kwargs={"org_slug": nomination.organisation.slug,
                                    "election_id": nomination.election_id})
    send_mail(
        subject=f"Please confirm a nomination: {nomination.election.title}",
        message=render_to_string("nominations/email/confirm_request.txt", {
            "member": member, "role": role, "nomination": nomination, "link": link}),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[member.email],
    )


def submit(election, *, submitter: Member, nominee: Member, proposer: Member | None,
           seconder: Member | None, statement: str, actor) -> Nomination:
    if election.state != lifecycle.NOMINATION:
        raise NominationError("Nominations are not open for this election.")
    rules = _rules(election)["nomination"]
    self_nominated = submitter.id == nominee.id
    if self_nominated and not rules["self_nomination"]:
        raise NominationError("This election does not accept self-nominations: another member "
                              "must nominate you as your proposer.")
    if not self_nominated:
        if proposer is not None and proposer.id != submitter.id:
            raise NominationError("When you nominate someone else, you are their proposer.")
        proposer = submitter
    if rules["proposer_required"] and proposer is None:
        raise NominationError("A proposer is required.")
    if rules["seconder_required"] and seconder is None:
        raise NominationError("A seconder is required.")
    people = [p.id for p in (nominee, proposer, seconder) if p is not None]
    if len(people) != len(set(people)):
        raise NominationError("The nominee, proposer and seconder must be different people.")
    _check_eligible(nominee, election, "nominee")
    for member, role in ((proposer, "proposer"), (seconder, "seconder")):
        if member is not None:
            _check_eligible(member, election, role)
    if Nomination.objects.filter(election=election, nominee=nominee).exclude(
            status=Nomination.Status.WITHDRAWN).exists():
        raise NominationError("This member already has a nomination in this election.")

    now = timezone.now()
    nomination = Nomination.objects.create(
        organisation_id=election.organisation_id, election=election, nominee=nominee,
        proposer=proposer, seconder=seconder, submitted_by=submitter,
        statement=statement.strip()[:4000],
        nominee_consented_at=now if self_nominated else None,
        proposer_confirmed_at=now if proposer is not None and proposer.id == submitter.id
        else None,
    )
    audit("nomination.submitted", actor=actor, organisation_id=election.organisation_id,
          object_type="nomination", object_id=nomination.id,
          details={"election_id": str(election.id), "self_nominated": self_nominated})
    if not self_nominated:
        _notify(nominee, nomination, "nominee")
    if proposer is not None and proposer.id != submitter.id:
        _notify(proposer, nomination, "proposer")
    if seconder is not None:
        _notify(seconder, nomination, "seconder")
    return nomination


def _require_open(nomination: Nomination, what: str) -> None:
    if nomination.election.state != lifecycle.NOMINATION:
        raise NominationError(f"{what} is only possible while nominations are open.")


def roles_of(member: Member, nomination: Nomination) -> list[str]:
    return [role for role, mid in (("nominee", nomination.nominee_id),
                                   ("proposer", nomination.proposer_id),
                                   ("seconder", nomination.seconder_id)) if mid == member.id]


def confirm(nomination: Nomination, member: Member, *, actor) -> None:
    _require_open(nomination, "Confirming a nomination")
    if nomination.status != Nomination.Status.SUBMITTED:
        raise NominationError("This nomination can no longer be confirmed.")
    now = timezone.now()
    fields = []
    for role in roles_of(member, nomination):
        attr = {"nominee": "nominee_consented_at", "proposer": "proposer_confirmed_at",
                "seconder": "seconder_confirmed_at"}[role]
        if getattr(nomination, attr) is None:
            setattr(nomination, attr, now)
            fields.append(attr)
    if not fields:
        raise NominationError("There is nothing for you to confirm on this nomination.")
    nomination.save(update_fields=fields)
    audit("nomination.confirmed", actor=actor, organisation_id=nomination.organisation_id,
          object_type="nomination", object_id=nomination.id,
          details={"as": roles_of(member, nomination)})


def withdraw(nomination: Nomination, member: Member, *, actor, declined: bool = False) -> None:
    """Withdraw (nominee or submitter) or decline (anyone named) a nomination."""
    roles = roles_of(member, nomination)
    allowed = roles if declined else (
        ["nominee"] if "nominee" in roles else
        (["submitter"] if nomination.submitted_by_id == member.id else []))
    if not allowed:
        raise NominationError("You cannot withdraw this nomination.")
    if nomination.status not in (Nomination.Status.SUBMITTED, Nomination.Status.APPROVED):
        raise NominationError("This nomination can no longer be withdrawn.")
    if nomination.election.state not in (lifecycle.NOMINATION, lifecycle.REVIEW):
        raise NominationError("The candidate list is fixed: contact the Returning Officer.")
    if nomination.status == Nomination.Status.APPROVED:
        Candidate.objects.filter(nomination=nomination).delete()
        _end_candidate_role(nomination)
    nomination.status = Nomination.Status.WITHDRAWN
    nomination.status_reason = (f"Declined by the {allowed[0]}" if declined
                                else f"Withdrawn by the {allowed[0]}")
    nomination.save(update_fields=["status", "status_reason"])
    audit("nomination.withdrawn", actor=actor, organisation_id=nomination.organisation_id,
          object_type="nomination", object_id=nomination.id,
          details={"reason": nomination.status_reason})


def update_statement(nomination: Nomination, member: Member, statement: str, *, actor) -> None:
    if member.id != nomination.nominee_id:
        raise NominationError("Only the nominee can edit the statement.")
    _require_open(nomination, "Editing the statement")
    nomination.statement = statement.strip()[:4000]
    nomination.save(update_fields=["statement"])
    audit("nomination.statement_updated", actor=actor,
          organisation_id=nomination.organisation_id, object_type="nomination",
          object_id=nomination.id)


def sniff_mime(head: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _MAGIC:
        if head.startswith(magic):
            return mime, ext
    return None


def add_document(nomination: Nomination, member: Member, upload, *, actor) -> NominationDocument:
    """Store a supporting file. The type is decided from its content, never the browser's claim."""
    if member.id not in (nomination.nominee_id, nomination.submitted_by_id):
        raise NominationError("Only the nominee or the person who submitted the nomination "
                              "can add documents.")
    if nomination.status != Nomination.Status.SUBMITTED:
        raise NominationError("Documents can only be added to a pending nomination.")
    _require_open(nomination, "Adding a document")
    if nomination.documents.count() >= MAX_DOCUMENTS:
        raise NominationError(f"At most {MAX_DOCUMENTS} documents per nomination.")
    data = upload.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise NominationError("The file is larger than 5 MB.")
    if not data:
        raise NominationError("The file is empty.")
    sniffed = sniff_mime(data[:16])
    if sniffed is None:
        raise NominationError("Upload a PDF, JPEG or PNG file.")
    mime, ext = sniffed
    storage_name = default_storage.save(
        f"nominations/{nomination.organisation_id}/{uuid.uuid4().hex}{ext}", ContentFile(data))
    original = os.path.basename(getattr(upload, "name", "") or "document")[:200] or "document"
    document = NominationDocument.objects.create(
        organisation_id=nomination.organisation_id, nomination=nomination,
        storage_name=storage_name, original_name=original,
        sha256=hashlib.sha256(data).hexdigest(), mime=mime, size_bytes=len(data),
        uploaded_by=actor)
    audit("nomination.document_added", actor=actor, organisation_id=nomination.organisation_id,
          object_type="nomination", object_id=nomination.id,
          details={"document_id": str(document.id), "sha256": document.sha256, "mime": mime})
    return document


def _candidate_role(nomination: Nomination, *, actor) -> None:
    user_id = nomination.nominee.user_id
    if user_id is None:
        return
    membership, _ = OrgMembership.objects.get_or_create(
        organisation_id=nomination.organisation_id, user_id=user_id)
    RoleAssignment.objects.create(organisation_id=nomination.organisation_id,
                                  membership=membership, role=Role.CANDIDATE,
                                  election_id=nomination.election_id, granted_by=actor)


def _end_candidate_role(nomination: Nomination) -> None:
    RoleAssignment.objects.filter(
        membership__user_id=nomination.nominee.user_id, role=Role.CANDIDATE,
        election_id=nomination.election_id, valid_to__isnull=True,
    ).update(valid_to=timezone.now())


def decide(nomination: Nomination, *, approve: bool, reason: str, actor) -> None:
    """Approve or reject a nomination (Returning Officer, election in review)."""
    election = nomination.election
    if election.state != lifecycle.REVIEW:
        raise NominationError("Nominations can only be decided while the election is in review.")
    if nomination.status == Nomination.Status.WITHDRAWN:
        raise NominationError("This nomination was withdrawn.")
    reason = reason.strip()[:1000]
    target = Nomination.Status.APPROVED if approve else Nomination.Status.REJECTED
    if nomination.status == target:
        raise NominationError(f"This nomination is already {target}.")
    if approve:
        if nomination.outstanding_confirmations:
            raise NominationError("Still waiting for confirmation from: "
                                  + ", ".join(nomination.outstanding_confirmations) + ".")
        if _rules(election)["nomination"]["documents_required"] and \
                not nomination.documents.exists():
            raise NominationError("This election requires a supporting document.")
        if nomination.documents.filter(scan_status=NominationDocument.Scan.INFECTED).exists():
            raise NominationError("A document failed the malware scan.")
        nominee = Member.objects.get(pk=nomination.nominee_id)
        _check_eligible(nominee, election, "nominee")
    elif not reason:
        raise NominationError("A reason is required to reject a nomination.")

    if nomination.status == Nomination.Status.APPROVED:  # reversing an approval
        Candidate.objects.filter(nomination=nomination).delete()
        _end_candidate_role(nomination)
    nomination.status = target
    nomination.status_reason = reason
    nomination.decided_by = actor
    nomination.decided_at = timezone.now()
    nomination.save(update_fields=["status", "status_reason", "decided_by", "decided_at"])
    if approve:
        next_order = (Candidate.objects.filter(election=election)
                      .aggregate(n=Max("ballot_order"))["n"] or 0) + 1
        Candidate.objects.create(organisation_id=nomination.organisation_id, election=election,
                                 nomination=nomination, display_name=nomination.nominee.full_name,
                                 statement=nomination.statement, ballot_order=next_order)
        _candidate_role(nomination, actor=actor)
    audit("nomination.decided", actor=actor, organisation_id=nomination.organisation_id,
          object_type="nomination", object_id=nomination.id,
          details={"decision": target, **({"reason": reason} if reason else {})})


def candidate_list_digest(candidates) -> str:
    canonical = [[c.ballot_order, str(c.id), c.display_name] for c in candidates]
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()


@register_transition_hook
def lifecycle_hook(election, target: str, actor, context: dict) -> None:
    """Nomination rules that gate election transitions."""
    if target == lifecycle.NOMINATION and election.current_rule_version_id is None:
        raise ElectionError("Set the election rules before opening nominations: they decide "
                            "who may be nominated and how.")
    if target != lifecycle.READY:
        return
    if Nomination.objects.filter(election=election,
                                 status=Nomination.Status.SUBMITTED).exists():
        raise ElectionError("Decide every pending nomination before submitting for approval.")
    approved = set(Nomination.objects.filter(election=election,
                                             status=Nomination.Status.APPROVED)
                   .values_list("id", flat=True))
    candidates = list(Candidate.objects.filter(election=election))
    if not candidates:
        raise ElectionError("There are no approved candidates.")
    if {c.nomination_id for c in candidates} != approved:  # pragma: no cover - defensive
        raise ElectionError("The candidate list does not match the approved nominations.")
    # Fix the ballot order now, as the approved rules say.
    if election.current_rule_version.rules["candidate_order"] == "random":
        secrets.SystemRandom().shuffle(candidates)
    else:
        candidates.sort(key=lambda c: (c.display_name.casefold(), str(c.id)))
    for position, candidate in enumerate(candidates, start=1):
        if candidate.ballot_order != position:
            Candidate.objects.filter(pk=candidate.pk).update(ballot_order=position)
            candidate.ballot_order = position
    audit("election.candidates_fixed", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"count": len(candidates), "seats": election.seats,
                   "uncontested": len(candidates) <= election.seats,
                   "order": [str(c.id) for c in candidates],
                   "digest": candidate_list_digest(candidates)})


def nominations_for_member(election, member: Member):
    return (Nomination.objects.filter(election=election)
            .filter(Q(nominee=member) | Q(proposer=member) | Q(seconder=member)
                    | Q(submitted_by=member))
            .select_related("nominee", "proposer", "seconder", "submitted_by")
            .order_by("-created_at"))

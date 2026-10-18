"""Quorum: assess the approved rule, and record attendance when it is the basis.

The system reports whether the quorum rule is met; it does not decide whether a
meeting or result is valid (BLUEPRINT A8).
"""

import math
from fractions import Fraction

from audit.services import record as audit
from elections import lifecycle
from electorate.models import ElectorateEntry
from members.models import Member

from .models import ParticipationRecord

ATTENDANCE_STATES = (lifecycle.OPEN, lifecycle.CLOSED, lifecycle.COUNTED, lifecycle.AWAITING)


class QuorumError(ValueError):
    pass


def assess(rule: dict, *, electorate: int, ballots: int, attendance: int) -> dict:
    """The outcome of a quorum rule ({"type", "basis", "threshold"}) for these numbers."""
    kind = rule.get("type", "none")
    if kind == "none":
        return {"type": "none", "met": True, "text": "No quorum requirement."}
    basis = rule["basis"]
    threshold = rule["threshold"]
    participants = ballots if basis == "accepted_ballots" else attendance
    if kind == "absolute":
        required = int(threshold)
    else:  # percent_of_electorate: at least threshold% of the electorate, rounded up
        required = math.ceil(Fraction(str(threshold)) * electorate / 100)
    met = participants >= required
    measure = ("accepted ballots" if basis == "accepted_ballots"
               else "members registered as attending")
    rule_text = (f"at least {threshold} {measure}" if kind == "absolute"
                 else f"at least {threshold:g}% of the electorate of {electorate} ({required}), "
                      f"measured by {measure}")
    return {"type": kind, "basis": basis, "threshold": threshold, "electorate": electorate,
            "participants": participants, "required": required, "met": met,
            "text": f"Quorum {'met' if met else 'NOT met'}: {participants} {measure}; the rule "
                    f"requires {rule_text}."}


def attendance_count(election) -> int:
    return ParticipationRecord.objects.filter(election=election).count()


def for_tally(election, tally) -> dict:
    return assess(election.current_rule_version.rules.get("quorum") or {"type": "none"},
                  electorate=tally.electorate_size, ballots=tally.ballots_total,
                  attendance=attendance_count(election))


def record_attendance(election, member_numbers: list[str], *, actor) -> tuple[int, list[str]]:
    """Record attendance by member number. Returns (added, numbers not in the electorate)."""
    if election.state not in ATTENDANCE_STATES:
        raise QuorumError("Attendance can be recorded from the opening of voting until "
                          "certification.")
    numbers = list(dict.fromkeys(n.strip() for n in member_numbers if n.strip()))
    electorate = ElectorateEntry.objects.filter(election=election).values("member_id")
    members = {m.member_number: m for m in Member.objects.filter(
        member_number__in=numbers, id__in=electorate)} if numbers else {}
    existing = set(ParticipationRecord.objects.filter(election=election)
                   .values_list("member_id", flat=True))
    added = [m for m in members.values() if m.id not in existing]
    ParticipationRecord.objects.bulk_create([
        ParticipationRecord(organisation_id=election.organisation_id, election=election,
                            member=m, recorded_by=actor) for m in added])
    if added:
        audit("quorum.attendance_recorded", actor=actor, organisation_id=election.organisation_id,
              object_type="election", object_id=election.id,
              details={"added": len(added), "total": attendance_count(election)})
    return len(added), [n for n in numbers if n not in members]


def remove_attendance(election, record_id, *, actor) -> None:
    if election.state not in ATTENDANCE_STATES:
        raise QuorumError("Attendance can no longer be changed.")
    deleted, _ = ParticipationRecord.objects.filter(election=election, pk=record_id).delete()
    if not deleted:
        raise QuorumError("That attendance record does not exist.")
    audit("quorum.attendance_removed", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"total": attendance_count(election)})

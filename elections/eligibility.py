"""Who satisfies an election's eligibility rules.

Used to check nominees, proposers and seconders (6.4) and, in increment 6.5, to
build the frozen electorate snapshot. Kept in one place so both apply exactly
the same rules.
"""

import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...]  # why not, in plain language (empty when eligible)


def evaluate(member, rules: dict, org_timezone: str) -> EligibilityResult:
    """Apply ``rules['eligibility']`` to ``member``.

    A member counts as having joined at 00:00 (organisation time) on their
    ``joined_on`` date; with a cut-off they must have joined strictly before it.
    """
    elig = rules["eligibility"]
    reasons = []
    if member.status not in elig["member_status"]:
        reasons.append("membership is not active")
    if member.category not in elig["categories"]:
        reasons.append(f"category “{member.category}” is not eligible")
    if member.subscription_status not in elig["subscription_status"]:
        reasons.append(f"subscription status “{member.subscription_status}” is not eligible")
    cutoff = elig.get("joined_before")
    if cutoff:
        tz = zoneinfo.ZoneInfo(org_timezone)
        joined_at = datetime.combine(member.joined_on, time.min, tzinfo=tz)
        if joined_at >= datetime.fromisoformat(cutoff):
            reasons.append("joined on or after the membership cut-off")
    return EligibilityResult(eligible=not reasons, reasons=tuple(reasons))

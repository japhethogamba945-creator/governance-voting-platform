"""Election rules: a versioned, validated JSON document (docs/design/03-database-design.md §4).

Nothing about any particular constitution is hard-coded: every governance choice
is a setting, validated here and frozen (approved) before the election is
scheduled.
"""

import json
from datetime import datetime

SCHEMA = "election-rules/v1"
COUNTING_METHODS = {"plurality_multi_seat": "Plurality (highest-polling candidates win the seats)"}
TIE_PROCEDURES = {
    "lot_drawn_by_returning_officer": "Lots drawn by the Returning Officer",
    "runoff_election": "A run-off election between the tied candidates",
    "chair_casting_vote": "Casting vote of the chair",
    "other": "Other procedure (described below)",
}
QUORUM_TYPES = {
    "none": "No quorum requirement",
    "absolute": "A minimum number of participants",
    "percent_of_electorate": "A minimum percentage of the electorate",
}
QUORUM_BASES = {
    "accepted_ballots": "accepted ballots",
    "registered_attendance": "registered attendance",
}
CANDIDATE_ORDERS = {
    "alphabetical": "Alphabetical by name",
    "random": "Random order, fixed when the election is scheduled",
}
SUBSCRIPTION_STATUSES = ("paid", "unpaid", "exempt", "n/a")


def default_rules(seats: int, categories: tuple[str, ...]) -> dict:
    return {
        "schema": SCHEMA,
        "counting_method": "plurality_multi_seat",
        "min_selections": 0,
        "max_selections": seats,
        "partial_ballots_allowed": True,
        "blank_ballot_allowed": True,
        "eligibility": {
            "member_status": ["active"],
            "categories": list(categories),
            "subscription_status": ["paid", "exempt"],
            "joined_before": None,
        },
        "nomination": {
            "self_nomination": True,
            "proposer_required": False,
            "seconder_required": False,
            "documents_required": False,
        },
        "tie_procedure": {"type": "lot_drawn_by_returning_officer", "description": ""},
        "quorum": {"type": "none", "basis": "accepted_ballots", "threshold": 0},
        "early_close_allowed": False,
        "turnout_visible_to_members": False,
        "candidate_order": "alphabetical",
    }


def canonical_json(rules: dict) -> str:
    return json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_rules(rules: dict, seats: int, categories: tuple[str, ...]) -> list[str]:
    """Return human-readable problems; an empty list means the rules are usable."""
    errors: list[str] = []

    def need(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    try:
        need(rules.get("schema") == SCHEMA, "Unknown rules format.")
        need(rules.get("counting_method") in COUNTING_METHODS, "Unknown counting method.")
        lo, hi = rules.get("min_selections"), rules.get("max_selections")
        need(isinstance(lo, int) and isinstance(hi, int), "Selection limits must be whole numbers.")
        if isinstance(lo, int) and isinstance(hi, int):
            need(1 <= hi <= seats,
                 f"Members may choose at most between 1 and {seats} candidates (the seats).")
            need(0 <= lo <= hi, "The minimum number of choices must be between 0 and the maximum.")
            partial = rules.get("partial_ballots_allowed")
            need(partial or lo == hi,
                 "If partial ballots are not allowed, the minimum must equal the maximum.")
            need(not rules.get("blank_ballot_allowed") or lo == 0,
                 "A blank ballot needs a minimum of 0 choices.")

        eligibility = rules.get("eligibility") or {}
        need(eligibility.get("member_status") == ["active"], "Only active members can be eligible.")
        cats = eligibility.get("categories") or []
        need(bool(cats), "Choose at least one eligible membership category.")
        need(set(cats) <= set(categories), "Unknown membership category in eligibility.")
        subs = eligibility.get("subscription_status") or []
        need(bool(subs), "Choose at least one eligible subscription status.")
        need(set(subs) <= set(SUBSCRIPTION_STATUSES), "Unknown subscription status.")
        cutoff = eligibility.get("joined_before")
        if cutoff is not None:
            try:
                parsed = datetime.fromisoformat(cutoff)
                need(parsed.tzinfo is not None, "The membership cut-off needs a time zone.")
            except (TypeError, ValueError):
                errors.append("The membership cut-off is not a valid date and time.")

        tie = rules.get("tie_procedure") or {}
        need(tie.get("type") in TIE_PROCEDURES, "Choose a tie-break procedure.")
        need(len((tie.get("description") or "").strip()) >= 10,
             "Describe the tie-break procedure as approved before the election "
             "(at least a sentence).")

        quorum = rules.get("quorum") or {}
        q_type = quorum.get("type")
        need(q_type in QUORUM_TYPES, "Choose a quorum rule.")
        need(quorum.get("basis") in QUORUM_BASES, "Choose what the quorum is measured against.")
        threshold = quorum.get("threshold")
        if q_type == "absolute":
            need(isinstance(threshold, int) and threshold >= 1,
                 "An absolute quorum must be at least 1.")
        elif q_type == "percent_of_electorate":
            need(isinstance(threshold, int | float) and 0 < threshold <= 100,
                 "A percentage quorum must be greater than 0 and at most 100.")

        need(rules.get("candidate_order") in CANDIDATE_ORDERS, "Choose a candidate order.")
        for flag in ("early_close_allowed", "turnout_visible_to_members",
                     "partial_ballots_allowed", "blank_ballot_allowed"):
            need(isinstance(rules.get(flag), bool), f"{flag} must be yes or no.")
        nomination = rules.get("nomination") or {}
        for flag in ("self_nomination", "proposer_required", "seconder_required",
                     "documents_required"):
            need(isinstance(nomination.get(flag), bool), f"nomination.{flag} must be yes or no.")
    except (AttributeError, TypeError):
        errors.append("The rules document is malformed.")
    return errors


def describe(rules: dict, seats: int) -> list[str]:
    """Plain-language summary for review screens."""
    lines = [
        f"{seats} seat{'s' if seats != 1 else ''} to fill. "
        f"{COUNTING_METHODS.get(rules['counting_method'], rules['counting_method'])}.",
    ]
    lo, hi = rules["min_selections"], rules["max_selections"]
    if lo == hi:
        lines.append(f"Each member chooses exactly {hi} candidate{'s' if hi != 1 else ''}.")
    else:
        lines.append(f"Each member may choose up to {hi} candidates"
                     + (f" and at least {lo}." if lo else "."))
    lines.append("Blank ballots are allowed." if rules["blank_ballot_allowed"]
                 else "Blank ballots are not allowed.")
    elig = rules["eligibility"]
    cutoff = elig.get("joined_before")
    lines.append(
        "Eligible voters: active members in categories "
        + ", ".join(elig["categories"])
        + " with subscription status " + ", ".join(elig["subscription_status"])
        + (f", who joined before {cutoff}." if cutoff else ".")
    )
    tie = rules["tie_procedure"]
    lines.append(f"Ties: {TIE_PROCEDURES[tie['type']]}. {tie['description'].strip()}")
    quorum = rules["quorum"]
    if quorum["type"] == "none":
        lines.append("No quorum requirement.")
    elif quorum["type"] == "absolute":
        lines.append(f"Quorum: at least {quorum['threshold']} "
                     f"{QUORUM_BASES[quorum['basis']]}.")
    else:
        lines.append(f"Quorum: {quorum['threshold']:g}% of the electorate, measured by "
                     f"{QUORUM_BASES[quorum['basis']]}.")
    lines.append("Voting may be closed early." if rules["early_close_allowed"]
                 else "Voting cannot be closed early.")
    lines.append(f"Candidate order: {CANDIDATE_ORDERS[rules['candidate_order']]}.")
    return lines

"""Counting ballots and allocating seats: pure functions, no database.

Plurality, multi-seat (docs/design/03-database-design.md §4): each valid ballot
gives one vote to each candidate it names; the highest-polling candidates win
the seats. A tie that decides who takes the last seat(s) is never broken here:
those candidates are "tied_pending" until the Returning Officer records the
outcome of the approved tie procedure.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import nacl.exceptions

from voting import encoding
from voting.services import BallotRejected, validate_selections

ELECTED, NOT_ELECTED, TIED = "elected", "not_elected", "tied_pending"
BOUNDARY_TIE_GROUP = 1


@dataclass(frozen=True)
class Line:
    candidate_id: uuid.UUID
    votes: int
    rank: int
    outcome: str
    tie_group: int | None = None


@dataclass(frozen=True)
class Count:
    ballots_total: int
    ballots_valid: int
    ballots_invalid: int
    ballots_blank: int
    lines: list[Line]

    @property
    def has_ties(self) -> bool:
        return any(line.outcome == TIED for line in self.lines)


def read_ballot(private_key, ciphertext: bytes, election_id: uuid.UUID,
                candidate_ids: set[uuid.UUID], rules: dict) -> list[uuid.UUID] | None:
    """The candidates a ballot names, or None if it is invalid.

    Every ballot was validated before it was accepted, so an invalid ballot here
    means tampering or a bug (docs/design/05-security-review.md §4).
    """
    try:
        content = encoding.decode(encoding.unseal(private_key, ciphertext))
        if content.get("v") != 1 or content.get("e") != str(election_id):
            return None
        return validate_selections(list(content.get("s", [])), candidate_ids, rules)
    except (nacl.exceptions.CryptoError, ValueError, TypeError, AttributeError,
            BallotRejected):
        return None


def allocate(votes: list[tuple[uuid.UUID, int]], seats: int) -> list[Line]:
    """Rank candidates and allocate ``seats``. ``votes`` is in ballot order.

    Returns lines by votes (highest first), ballot order among equals. Rank is
    "1 + the number of candidates with more votes", so equal votes share a rank.
    """
    ordered = sorted(votes, key=lambda cv: -cv[1])  # stable: ballot order among equals
    if len(ordered) <= seats:
        return [Line(c, v, 1 + sum(1 for _, o in ordered if o > v), ELECTED) for c, v in ordered]
    boundary = ordered[seats - 1][1]
    above = sum(1 for _, v in ordered if v > boundary)
    level = sum(1 for _, v in ordered if v == boundary)
    tie = above + level > seats  # more candidates level at the boundary than seats left
    lines = []
    for candidate, v in ordered:
        rank = 1 + sum(1 for _, o in ordered if o > v)
        if v > boundary or (v == boundary and not tie):
            lines.append(Line(candidate, v, rank, ELECTED))
        elif v == boundary:
            lines.append(Line(candidate, v, rank, TIED, BOUNDARY_TIE_GROUP))
        else:
            lines.append(Line(candidate, v, rank, NOT_ELECTED))
    return lines


def seats_in_tie(lines: list[Line], seats: int) -> int:
    """How many seats the tied candidates share."""
    return seats - sum(1 for line in lines if line.outcome == ELECTED)


def count(private_key, ciphertexts: Iterable[bytes], *, election_id: uuid.UUID,
          candidates: list[uuid.UUID], rules: dict, seats: int) -> Count:
    """Decrypt, validate and aggregate. Individual ballots are never kept or returned."""
    candidate_set = set(candidates)
    totals = dict.fromkeys(candidates, 0)
    total = valid = blank = 0
    for ciphertext in ciphertexts:
        total += 1
        chosen = read_ballot(private_key, ciphertext, election_id, candidate_set, rules)
        if chosen is None:
            continue
        valid += 1
        if not chosen:
            blank += 1
        for candidate in chosen:
            totals[candidate] += 1
    return Count(ballots_total=total, ballots_valid=valid, ballots_invalid=total - valid,
                 ballots_blank=blank,
                 lines=allocate([(c, totals[c]) for c in candidates], seats))


def resolved_outcomes(lines: list[Line], elected_in_tie: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Final outcome per candidate once the tie procedure's result is recorded."""
    return {line.candidate_id: (line.outcome if line.outcome != TIED
                                else ELECTED if line.candidate_id in elected_in_tie
                                else NOT_ELECTED)
            for line in lines}

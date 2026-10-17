"""The election state machine (docs/design/BLUEPRINT.md §7).

The database enforces *which* transitions exist (``allowed_transition``, kept
identical to this table by a test), requires reasons where marked, and records
every transition itself. This module adds *who* may perform each one, whether
it needs step-up authentication, and whether its preconditions are built yet.
"""

from dataclasses import dataclass

from tenancy.roles import Role

DRAFT, NOMINATION, REVIEW, READY = "draft", "nomination", "review", "ready_for_approval"
SCHEDULED, OPEN, CLOSED, COUNTED = "scheduled", "open", "closed", "counted"
AWAITING, CERTIFIED = "awaiting_certification", "certified"
SUSPENDED, DISPUTED, CANCELLED = "suspended", "disputed", "cancelled"

STATES = (DRAFT, NOMINATION, REVIEW, READY, SCHEDULED, OPEN, CLOSED, COUNTED, AWAITING,
          CERTIFIED, SUSPENDED, DISPUTED, CANCELLED)
STATE_LABELS = {
    DRAFT: "Draft", NOMINATION: "Nominations open", REVIEW: "Reviewing nominations",
    READY: "Awaiting approval", SCHEDULED: "Scheduled", OPEN: "Voting open",
    CLOSED: "Voting closed", COUNTED: "Counted", AWAITING: "Awaiting certification",
    CERTIFIED: "Certified", SUSPENDED: "Suspended", DISPUTED: "Disputed",
    CANCELLED: "Cancelled",
}
# Configuration (title, seats, times, rules) may only change in these states.
EDITABLE_STATES = frozenset({DRAFT, NOMINATION, REVIEW})

RO, ADMIN = Role.RETURNING_OFFICER, Role.ORG_ADMIN
SYSTEM: frozenset = frozenset()  # performed by the scheduler, not a person


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    label: str
    roles: frozenset
    reason_required: bool = False
    step_up: bool = False
    # False until the increment that implements the transition's preconditions.
    available: bool = True
    note: str = ""
    # Performed by a dedicated workflow (counting, certification) that checks its
    # own preconditions, never by the generic transition form.
    flow: bool = False


_ALL = [
    Transition(DRAFT, NOMINATION, "Open nominations", frozenset({ADMIN, RO})),
    Transition(NOMINATION, REVIEW, "Close nominations", frozenset({RO})),
    Transition(REVIEW, READY, "Submit for approval", frozenset({RO})),
    Transition(READY, DRAFT, "Return to draft", frozenset({RO}), reason_required=True),
    Transition(READY, SCHEDULED, "Approve and schedule", frozenset({RO}), step_up=True,
               note="Includes the key ceremony: choose two other keyholders."),
    Transition(SCHEDULED, OPEN, "Open voting", SYSTEM,
               note="Opened automatically by the scheduler at the opening time."),
    Transition(OPEN, CLOSED, "Close voting", SYSTEM,
               note="Closed automatically by the scheduler at the closing time."),
    Transition(OPEN, SUSPENDED, "Suspend voting", frozenset({RO}), reason_required=True,
               step_up=True),
    Transition(SUSPENDED, OPEN, "Resume voting", frozenset({RO}), reason_required=True,
               step_up=True),
    Transition(CLOSED, COUNTED, "Count", frozenset({RO}), flow=True,
               note="On the counting page: two keyholders provide their shares."),
    Transition(COUNTED, AWAITING, "Reconciliation passed", SYSTEM, flow=True,
               note="Automatic once reconciliation passes and any tie is resolved."),
    Transition(AWAITING, COUNTED, "Order a recount", frozenset({RO}), reason_required=True,
               flow=True),
    Transition(AWAITING, CERTIFIED, "Certify", frozenset({RO}), step_up=True, flow=True),
    Transition(CERTIFIED, DISPUTED, "Mark disputed", frozenset({RO, ADMIN}),
               reason_required=True, step_up=True),
    Transition(DISPUTED, COUNTED, "Order a recount", frozenset({RO}), reason_required=True,
               flow=True),
    Transition(DISPUTED, CERTIFIED, "Re-certify", frozenset({RO}), step_up=True,
               note="Confirms the existing certified result without a recount."),
    Transition(DRAFT, CANCELLED, "Cancel election", frozenset({ADMIN, RO}),
               reason_required=True, step_up=True),
    Transition(NOMINATION, CANCELLED, "Cancel election", frozenset({RO}),
               reason_required=True, step_up=True),
    Transition(REVIEW, CANCELLED, "Cancel election", frozenset({RO}),
               reason_required=True, step_up=True),
    Transition(SCHEDULED, CANCELLED, "Cancel election", frozenset({RO}),
               reason_required=True, step_up=True),
    Transition(SUSPENDED, CANCELLED, "Cancel election", frozenset({RO}),
               reason_required=True, step_up=True, available=False,
               note="Needs a co-signature from an administrator (later increment)."),
]

TRANSITIONS: dict[tuple[str, str], Transition] = {(t.source, t.target): t for t in _ALL}


def available_for(state: str, roles) -> list[Transition]:
    """Transitions a person with ``roles`` can perform from ``state`` right now."""
    roles = set(roles)
    return [t for t in _ALL
            if t.source == state and t.available and not t.flow and t.roles & roles]

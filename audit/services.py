import uuid

from .models import AuditEvent

# Keys that could carry ballot contents or secrets are rejected outright.
FORBIDDEN_DETAIL_KEYS = frozenset({
    "selections", "selection", "choices", "choice", "candidate_ids", "ballot", "ciphertext",
    "receipt_secret", "idempotency_key", "password", "token", "share", "passphrase",
})


class ForbiddenAuditDetail(ValueError):
    pass


def _check_details(details: dict) -> None:
    for key, value in details.items():
        if key.lower() in FORBIDDEN_DETAIL_KEYS:
            raise ForbiddenAuditDetail(f"audit details may not contain {key!r}")
        if isinstance(value, dict):
            _check_details(value)


def record(action: str, *, actor=None, actor_role: str = "",
           organisation_id: uuid.UUID | None = None, object_type: str = "",
           object_id: uuid.UUID | None = None, details: dict | None = None,
           using: str = "default") -> AuditEvent:
    """Write an audit event in the caller's transaction."""
    details = details or {}
    _check_details(details)
    return AuditEvent.objects.using(using).create(
        action=action, actor=actor, actor_role=actor_role, organisation_id=organisation_id,
        object_type=object_type, object_id=object_id, details=details,
    )

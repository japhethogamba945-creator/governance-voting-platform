"""The audit hash chain: canonical form, hashing and verification (pure functions).

docs/design/05-security-review.md §6. Per organisation (and one chain for
platform-level events), events are numbered ``seq`` = 1, 2, 3... and

    hash = SHA-256(prev_hash || canonical JSON of the event)

with ``prev_hash`` of the first event = 32 zero bytes. The canonical JSON
follows RFC 8785 (JCS) for every value type the platform writes: sorted keys,
no whitespace, ES6 number formatting, minimal string escaping. The hashed fields
are listed in ``FIELDS``; changing any of them, removing an event or reordering
events breaks the chain from that point on.

``verify`` works on plain dictionaries, so the same code checks the live
database and an exported offline bundle.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

GENESIS = bytes(32)
FIELDS = ("id", "organisation_id", "seq", "at", "actor_id", "actor_role", "action",
          "object_type", "object_id", "details")
MAX_PROBLEMS = 50


def _number(value: float) -> str:
    """ES6 Number.prototype.toString, as RFC 8785 requires."""
    if not math.isfinite(value):
        raise ValueError("non-finite numbers cannot be canonicalised")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    text = repr(abs(value))
    mantissa, _, exponent = text.partition("e")
    whole, _, fraction = mantissa.partition(".")
    raw = whole + fraction
    leading = len(raw) - len(raw.lstrip("0"))
    digits = raw[leading:].rstrip("0")
    # value = 0.digits × 10^n
    k, n = len(digits), len(whole) + int(exponent or 0) - leading
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + ("e+" if e > 0 else "e-") \
            + str(abs(e))
    return sign + out


def canonical(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(f"{canonical(str(k))}:{canonical(value[k])}" for k in keys) + "}"
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def timestamp(value) -> str:
    if isinstance(value, str):
        return value
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def payload(event: dict) -> dict:
    """The hashed form of an event (identifiers as strings, time in UTC)."""
    def text(v):
        return None if v is None else str(v)

    return {"id": str(event["id"]), "organisation_id": text(event["organisation_id"]),
            "seq": int(event["seq"]), "at": timestamp(event["at"]),
            "actor_id": text(event["actor_id"]), "actor_role": event["actor_role"] or "",
            "action": event["action"], "object_type": event["object_type"] or "",
            "object_id": text(event["object_id"]), "details": event["details"] or {}}


def event_hash(prev_hash: bytes, event: dict) -> bytes:
    return hashlib.sha256(prev_hash + canonical(payload(event)).encode()).digest()


def _bytes(value) -> bytes | None:
    if value is None:
        return None
    return bytes.fromhex(value) if isinstance(value, str) else bytes(value)


@dataclass
class Verification:
    events: int = 0
    anchors: int = 0
    head_seq: int = 0
    head_hash: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def problem(self, text: str) -> None:
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(text)


def verify(events: list[dict], anchors: list[dict]) -> Verification:
    """Recompute one chain (events sorted by seq) and check every anchor against it."""
    result = Verification()
    hashes: dict[int, bytes] = {}
    prev = GENESIS
    for expected_seq, event in enumerate(events, start=1):
        seq = int(event["seq"])
        if seq != expected_seq:
            result.problem(f"Sequence gap or reordering: expected {expected_seq}, found {seq}.")
            prev = _bytes(event["prev_hash"]) or GENESIS  # resynchronise to keep checking
        stored_prev, stored = _bytes(event["prev_hash"]), _bytes(event["hash"])
        if stored_prev != prev:
            result.problem(f"Event {seq}: does not link to the previous event.")
        computed = event_hash(stored_prev if stored_prev is not None else prev, event)
        if stored != computed:
            result.problem(f"Event {seq} ({event['action']}): content does not match its hash.")
        hashes[seq] = stored or computed
        prev = stored or computed
        result.events += 1
        result.head_seq, result.head_hash = seq, prev.hex()
    for anchor in anchors:
        result.anchors += 1
        seq = int(anchor["up_to_seq"])
        if seq not in hashes:
            result.problem(f"Anchor at event {seq}: that event is missing.")
        elif hashes[seq].hex() != anchor["head_hash"]:
            result.problem(f"Anchor at event {seq}: the chain was changed after anchoring.")
    return result


def utcnow_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

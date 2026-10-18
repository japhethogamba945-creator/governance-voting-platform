"""The chaining job, anchoring and verification against the database.

``chain_and_anchor()`` runs from ``manage.py run_scheduler`` as ``app_audit``,
the only role that may set ``seq``/``prev_hash``/``hash`` and write anchors.
The job process must log in with a role that can ``SET ROLE app_audit``; the web
role cannot (db/roles.sql). One run at a time is ensured by an advisory lock.

Anchors are written when a chain has new events and either the last anchor is
older than ``AUDIT_ANCHOR_INTERVAL_MINUTES`` or a key event (voting opened or
closed, a count, certification, publication) was chained. Each anchor is also
written as a write-once file under ``AUDIT_ANCHOR_DIR``. Auditors are emailed
the chain head when voting opens or closes and when a result is certified.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.utils import timezone

from platform_core.db import acting_as, db_context

from . import chain
from .models import AuditAnchor

log = logging.getLogger(__name__)

LOCK_KEY = 0x677670617564  # "gvpaud"
BATCH = 5000
KEY_TRANSITIONS = ("open", "closed", "counted", "certified")
KEY_ACTIONS = ("tally.completed", "election.certified", "results.published")
NOTIFY_TRANSITIONS = {"open": "voting opened", "closed": "voting closed",
                      "certified": "a result was certified"}


@dataclass(frozen=True)
class Anchored:
    organisation_id: uuid.UUID | None
    up_to_seq: int
    head_hash: str
    reason: str
    notify: tuple[str, ...]


def _rows(cursor) -> list[dict]:
    columns = [c.name for c in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


_PENDING_EVENTS = (
    "SELECT id, organisation_id, seq, at, actor_id, actor_role, action, object_type, "
    "object_id, details::text AS details, prev_hash, hash FROM audit_event "
    "WHERE organisation_id IS NOT DISTINCT FROM %s AND hash IS NULL ORDER BY at, id LIMIT %s")
_CHAINED_EVENTS = (
    "SELECT id, organisation_id, seq, at, actor_id, actor_role, action, object_type, "
    "object_id, details::text AS details, prev_hash, hash FROM audit_event "
    "WHERE organisation_id IS NOT DISTINCT FROM %s AND seq IS NOT NULL ORDER BY seq")


def _load(rows: list[dict]) -> list[dict]:
    for row in rows:
        row["details"] = json.loads(row["details"]) if row["details"] else {}
    return rows


def _head(cursor, org_id) -> tuple[int, bytes]:
    cursor.execute("SELECT seq, hash FROM audit_event WHERE organisation_id "
                   "IS NOT DISTINCT FROM %s AND seq IS NOT NULL ORDER BY seq DESC LIMIT 1",
                   [org_id])
    row = cursor.fetchone()
    return (row[0], bytes(row[1])) if row else (0, chain.GENESIS)


def _chain_one(cursor, org_id) -> int:
    seq, prev = _head(cursor, org_id)
    cursor.execute(_PENDING_EVENTS, [org_id, BATCH])
    events = _load(_rows(cursor))
    for event in events:
        seq += 1
        event["seq"] = seq
        digest = chain.event_hash(prev, event)
        cursor.execute("UPDATE audit_event SET seq = %s, prev_hash = %s, hash = %s WHERE id = %s",
                       [seq, prev, digest, event["id"]])
        prev = digest
    return len(events)


def _write_file(org_id, up_to_seq: int, head_hash: str, reason: str, at) -> str:
    base = getattr(settings, "AUDIT_ANCHOR_DIR", "")
    if not base:
        return ""
    folder = Path(base) / (str(org_id) if org_id else "platform")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{up_to_seq:012d}-{head_hash[:16]}.json"
    body = chain.canonical({"organisation_id": str(org_id) if org_id else None,
                            "up_to_seq": up_to_seq, "head_hash": head_hash, "reason": reason,
                            "anchored_at": chain.timestamp(at)})
    with open(path, "x", encoding="utf-8") as handle:  # never overwrite an anchor
        handle.write(body + "\n")
    os.chmod(path, 0o444)
    return str(path)


def _anchor_if_due(cursor, org_id, now) -> Anchored | None:
    head_seq, head = _head(cursor, org_id)
    cursor.execute("SELECT up_to_seq, anchored_at FROM audit_anchor "
                   "WHERE organisation_id IS NOT DISTINCT FROM %s ORDER BY up_to_seq DESC LIMIT 1",
                   [org_id])
    last = cursor.fetchone()
    last_seq = last[0] if last else 0
    if head_seq == 0 or head_seq == last_seq:
        return None
    cursor.execute(
        "SELECT action, details->>'to' FROM audit_event WHERE organisation_id IS NOT DISTINCT "
        "FROM %s AND seq > %s AND (action = ANY(%s) OR (action = 'election.transition' "
        "AND details->>'to' = ANY(%s)))",
        [org_id, last_seq, list(KEY_ACTIONS), list(KEY_TRANSITIONS)])
    key_events = cursor.fetchall()
    interval = timedelta(minutes=getattr(settings, "AUDIT_ANCHOR_INTERVAL_MINUTES", 15))
    if not key_events and last is not None and now - last[1] < interval:
        return None
    reasons = sorted({f"election {to}" if action == "election.transition" else action
                      for action, to in key_events}) or ["scheduled"]
    reason = ", ".join(reasons)[:200]
    try:
        location = _write_file(org_id, head_seq, head.hex(), reason, now)
    except OSError:
        log.exception("Could not write the audit anchor file for %s", org_id or "platform")
        location = ""
    AuditAnchor.objects.create(organisation_id=org_id, up_to_seq=head_seq, head_hash=head.hex(),
                               reason=reason, location=location, anchored_at=now)
    notify = tuple(NOTIFY_TRANSITIONS[to] for action, to in key_events
                   if action == "election.transition" and to in NOTIFY_TRANSITIONS)
    return Anchored(org_id, head_seq, head.hex(), reason, notify)


def chain_and_anchor(now=None) -> dict:
    """Chain every pending event and anchor where due. Returns a summary."""
    now = now or timezone.now()
    chained: dict = {}
    anchored: list[Anchored] = []
    with transaction.atomic(), acting_as("app_audit"), connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [LOCK_KEY])
        if not cursor.fetchone()[0]:
            return {"chained": {}, "anchored": [], "skipped": "another run holds the lock"}
        cursor.execute("SELECT DISTINCT organisation_id FROM audit_event WHERE hash IS NULL")
        for (org_id,) in cursor.fetchall():
            chained[org_id] = _chain_one(cursor, org_id)
        cursor.execute("SELECT DISTINCT organisation_id FROM audit_event WHERE seq IS NOT NULL")
        for (org_id,) in cursor.fetchall():
            result = _anchor_if_due(cursor, org_id, now)
            if result:
                anchored.append(result)
    for anchor in anchored:
        if anchor.notify and anchor.organisation_id:
            _notify_auditors(anchor)
    return {"chained": chained, "anchored": anchored}


def _notify_auditors(anchor: Anchored) -> None:
    from django.db.models import Q

    from tenancy.models import Organisation, RoleAssignment
    from tenancy.roles import Role

    with transaction.atomic(), db_context(org_id=anchor.organisation_id):
        org = Organisation.objects.get(pk=anchor.organisation_id)
        now = timezone.now()
        emails = sorted(set(
            RoleAssignment.objects.filter(role=Role.AUDITOR, valid_from__lte=now)
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
            .values_list("membership__user__email", flat=True)))
    if not emails:
        return
    send_mail(
        subject=f"Audit chain head for {org.legal_name}",
        message=(f"Event: {', '.join(anchor.notify)}.\n\n"
                 f"Organisation: {org.legal_name}\n"
                 f"Audit events up to: {anchor.up_to_seq}\n"
                 f"Chain head (SHA-256): {anchor.head_hash}\n\n"
                 "Keep this message. Anyone can later check that the audit trail up to this "
                 "point is unchanged by comparing this value with the result of "
                 "'Verify the audit chain'.\n"),
        from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=emails)


# --- verification ------------------------------------------------------------------------

def chain_events(org_id) -> list[dict]:
    """Chained events of one organisation, in chain order (caller's organisation context)."""
    with connection.cursor() as cursor:
        cursor.execute(_CHAINED_EVENTS, [org_id])
        return _load(_rows(cursor))


def anchors_for(org_id) -> list[dict]:
    return list(AuditAnchor.objects.filter(organisation_id=org_id).order_by("up_to_seq")
                .values("up_to_seq", "head_hash", "reason", "location", "anchored_at"))


def unchained_count(org_id) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM audit_event WHERE organisation_id "
                       "IS NOT DISTINCT FROM %s AND hash IS NULL", [org_id])
        return cursor.fetchone()[0]


def check_anchor_files(org_id, anchors: list[dict], result: chain.Verification) -> None:
    """Every recorded anchor must match its write-once file, and every file the chain."""
    base = getattr(settings, "AUDIT_ANCHOR_DIR", "")
    if not base:
        result.problem("No anchor location is configured: anchors exist only in the database.")
        return
    folder = Path(base) / (str(org_id) if org_id else "platform")
    recorded = {a["up_to_seq"]: a["head_hash"] for a in anchors}
    on_disk = set()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result.problem(f"Anchor file {path.name} is unreadable.")
            continue
        seq = data.get("up_to_seq")
        on_disk.add(seq)
        if recorded.get(seq) != data.get("head_hash"):
            result.problem(f"Anchor file for event {seq} does not match the database: the audit "
                           "trail or its anchors were changed.")
    for seq in recorded:
        if seq not in on_disk:
            result.problem(f"The anchor at event {seq} has no anchor file.")


def verify_org(org_id) -> tuple[chain.Verification, int]:
    """Verify one organisation's chain, its anchors and the anchor files."""
    anchors = anchors_for(org_id)
    result = chain.verify(chain_events(org_id), anchors)
    check_anchor_files(org_id, anchors, result)
    return result, unchained_count(org_id)


def bundle(org) -> dict:
    """Everything needed to verify the chain offline (manage.py verify_audit --bundle)."""
    events = chain_events(org.id)
    return {
        "schema": "audit-bundle/v1",
        "organisation": {"id": str(org.id), "slug": org.slug, "name": org.legal_name},
        "exported_at": chain.utcnow_text(),
        "events": [{**chain.payload(e), "prev_hash": bytes(e["prev_hash"]).hex(),
                    "hash": bytes(e["hash"]).hex()} for e in events],
        "anchors": [{"up_to_seq": a["up_to_seq"], "head_hash": a["head_hash"],
                     "reason": a["reason"], "anchored_at": chain.timestamp(a["anchored_at"])}
                    for a in anchors_for(org.id)],
    }


def verify_bundle(data: dict) -> chain.Verification:
    return chain.verify(sorted(data["events"], key=lambda e: int(e["seq"])), data["anchors"])

"""The audit hash chain, anchoring, verification and the auditor views (acceptance test 8)."""

import json
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.utils import timezone

from audit import chain, chaining
from audit.models import AuditEvent
from audit.services import record
from platform_core.db import acting_as, db_context
from tenancy.roles import Role
from tests.factories import add_member, make_user
from tests.helpers import login

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def anchor_dir(settings, tmp_path):
    settings.AUDIT_ANCHOR_DIR = str(tmp_path / "anchors")
    return Path(settings.AUDIT_ANCHOR_DIR)


def _events(org, n=3, action="test.event"):
    with transaction.atomic(), db_context(org_id=org.id):
        return [record(action, organisation_id=org.id,
                       details={"i": i, "note": "café ✓", "ratio": 0.25}) for i in range(n)]


def _verify(org):
    with transaction.atomic(), db_context(org_id=org.id):
        return chaining.verify_org(org.id)


def _tamper(sql, params):
    """Superuser with triggers off (session_replication_role = replica)."""
    with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute(sql, params)
        cursor.execute("SET LOCAL session_replication_role = origin")


# --- canonical form ----------------------------------------------------------------------

def test_canonical_json_follows_rfc8785_for_the_types_we_write():
    assert chain.canonical({"b": 1, "a": [True, None, "é\n\u0001"], "c": {"z": 1.5, "y": 2.0}}) \
        == '{"a":[true,null,"é\\n\\u0001"],"b":1,"c":{"y":2,"z":1.5}}'
    for value, text in [(1e21, "1e+21"), (1e-7, "1e-7"), (1e-6, "0.000001"), (25.0, "25"),
                        (-0.5, "-0.5"), (123e18, "123000000000000000000"), (0.1, "0.1"),
                        (1.2345678901234568e+21, "1.2345678901234568e+21")]:
        assert chain.canonical(value) == text
    with pytest.raises(ValueError):
        chain.canonical(float("nan"))


# --- chaining and anchoring ------------------------------------------------------------------

def test_events_are_chained_per_organisation_and_verify(two_orgs, anchor_dir):
    a, b = two_orgs
    _events(a.org, 3)
    _events(b.org, 2)
    summary = chaining.chain_and_anchor()
    assert summary["chained"][a.org.id] >= 3 and summary["chained"][b.org.id] >= 2
    with acting_as(None):
        seqs = list(AuditEvent.objects.filter(organisation=a.org).order_by("seq")
                    .values_list("seq", flat=True))
        first = AuditEvent.objects.get(organisation=a.org, seq=1)
    assert seqs == list(range(1, len(seqs) + 1))
    assert bytes(first.prev_hash) == chain.GENESIS
    result, unchained = _verify(a.org)
    assert result.ok, result.problems
    assert unchained == 0 and result.head_seq == len(seqs) and result.anchors == 1
    # The first chaining anchors each organisation; the file is write-once and read-only.
    files = list((anchor_dir / str(a.org.id)).glob("*.json"))
    assert len(files) == 1 and not os.stat(files[0]).st_mode & stat.S_IWUSR
    assert json.loads(files[0].read_text())["head_hash"] == result.head_hash
    # Nothing new: no new anchor. New ordinary events: chained, anchored only after the interval.
    assert chaining.chain_and_anchor()["anchored"] == []
    _events(a.org, 1)
    assert chaining.chain_and_anchor()["anchored"] == []
    later = chaining.chain_and_anchor(now=timezone.now() + timedelta(minutes=16))
    assert [x.organisation_id for x in later["anchored"]] == [a.org.id]


def test_key_events_are_anchored_at_once_and_auditors_are_told(two_orgs):
    a, _ = two_orgs
    auditor, _ = make_user("auditor-chain@example.org")
    add_member(a.org, auditor, Role.AUDITOR)
    _events(a.org, 1)
    chaining.chain_and_anchor()
    mail.outbox.clear()
    with transaction.atomic(), db_context(org_id=a.org.id):
        record("election.transition", organisation_id=a.org.id, details={"from": "open",
                                                                         "to": "closed"})
    anchored = chaining.chain_and_anchor()["anchored"]
    assert [x.reason for x in anchored] == ["election closed"]
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["auditor-chain@example.org"]
    assert anchored[0].head_hash in mail.outbox[0].body


def test_platform_level_events_have_their_own_chain(two_orgs):
    with transaction.atomic(), db_context():
        record("auth.login_failed", details={"reason": "test"})
    chaining.chain_and_anchor()
    with acting_as(None):
        events = chaining.chain_events(None)
    assert events and chain.verify(events, []).ok


def test_only_the_chaining_role_can_chain_or_anchor(two_orgs):
    a, _ = two_orgs
    event = _events(a.org, 1)[0]
    with transaction.atomic(), db_context(org_id=a.org.id):
        for sql, params in [
            ("UPDATE audit_event SET seq = 1, prev_hash = %s, hash = %s WHERE id = %s",
             [b"x", b"y", event.id]),
            ("INSERT INTO audit_anchor (id, organisation_id, up_to_seq, head_hash, reason, "
             "location, anchored_at) VALUES (gen_random_uuid(), %s, 1, 'x', 'r', '', now())",
             [a.org.id]),
        ]:
            with pytest.raises(DatabaseError, match="permission denied"), \
                    transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql, params)
    chaining.chain_and_anchor()
    with acting_as(None):  # anchors are append-only, even for the owner
        for sql in ("UPDATE audit_anchor SET head_hash = 'x'", "DELETE FROM audit_anchor"):
            with pytest.raises(DatabaseError, match="append-only"), transaction.atomic(), \
                    connection.cursor() as cursor:
                cursor.execute(sql)


# --- tamper detection --------------------------------------------------------------------------

def test_changed_content_is_detected(two_orgs):
    a, _ = two_orgs
    events = _events(a.org, 3)
    chaining.chain_and_anchor()
    _tamper("UPDATE audit_event SET details = '{\"i\": 99}' WHERE id = %s", [events[1].id])
    result, _ = _verify(a.org)
    assert not result.ok and any("does not match its hash" in p for p in result.problems)


def test_a_removed_event_is_detected(two_orgs):
    a, _ = two_orgs
    events = _events(a.org, 3)
    chaining.chain_and_anchor()
    _tamper("DELETE FROM audit_event WHERE id = %s", [events[1].id])
    result, _ = _verify(a.org)
    assert not result.ok and any("gap" in p for p in result.problems)


def test_a_rewritten_chain_is_caught_by_its_anchor_file(two_orgs):
    """An attacker with superuser access rewrites an event, recomputes every later hash and
    even the anchor row in the database: the write-once anchor file still disagrees."""
    a, _ = two_orgs
    events = _events(a.org, 3)
    chaining.chain_and_anchor()
    with acting_as(None):
        rows = chaining.chain_events(a.org.id)
    target = next(r for r in rows if r["id"] == events[0].id)
    target["details"] = {"i": 0, "note": "forged"}
    prev = chain.GENESIS
    for row in rows:
        digest = chain.event_hash(prev, row)
        _tamper("UPDATE audit_event SET details = %s::jsonb, prev_hash = %s, hash = %s "
                "WHERE id = %s", [json.dumps(row["details"]), prev, digest, row["id"]])
        prev = digest
    _tamper("UPDATE audit_anchor SET head_hash = %s WHERE organisation_id = %s",
            [prev.hex(), a.org.id])
    result, _ = _verify(a.org)
    assert not result.ok
    assert any("does not match the database" in p for p in result.problems)


def test_verify_audit_command(two_orgs, tmp_path):
    a, _ = two_orgs
    events = _events(a.org, 2)
    chaining.chain_and_anchor()
    call_command("verify_audit", org=a.org.slug)
    with transaction.atomic(), db_context(org_id=a.org.id):
        exported = chaining.bundle(a.org)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(exported))
    call_command("verify_audit", bundle=str(path))
    exported["events"][0]["details"] = {"forged": True}
    path.write_text(json.dumps(exported))
    with pytest.raises(CommandError):
        call_command("verify_audit", bundle=str(path))
    _tamper("UPDATE audit_event SET action = 'x' WHERE id = %s", [events[0].id])
    with pytest.raises(CommandError):
        call_command("verify_audit", org=a.org.slug)


# --- the auditor views and acceptance test 8 -----------------------------------------------------

def test_admin_and_audit_views_disclose_no_choices(two_orgs):
    """Acceptance test 8 after voting and counting: the audit trail, the bundle and the
    administrator's pages show who did what, never how anyone voted."""
    from tests.test_tally import _count, _vote_and_close
    from tests.test_voting import build_open_election

    a, _ = two_orgs
    w = build_open_election(a.org, a.admin)
    _vote_and_close(w, [(0,), (0, 1)])
    _count(w)
    chaining.chain_and_anchor()
    base = f"/o/{a.org.slug}"
    ids = [str(c.id) for c in w["candidates"]]
    names = [c.display_name for c in w["candidates"]]

    admin = Client()
    login(admin, a.admin, a.admin_device)
    trail = admin.get(f"{base}/audit/").content.decode()
    assert "ballot.accepted" not in trail and "tally.completed" in trail  # D6: no register
    assert admin.get(f"{base}/audit/bundle.json").status_code == 403
    assert admin.get(f"{base}/elections/{w['election'].id}/marked-register/").status_code == 403
    # Before publication the administrator sees no per-candidate totals at all.
    for page in (admin.get(f"{base}/elections/{w['election'].id}/").content.decode(),
                 admin.get(f"{base}/elections/{w['election'].id}/count/").content.decode()):
        assert not any(name in page for name in names)

    ro = Client()
    login(ro, *w["ro"])
    assert "ballot.accepted" in ro.get(f"{base}/audit/").content.decode()  # RO and Auditor
    bundle = json.loads(ro.get(f"{base}/audit/bundle.json").content)
    assert chaining.verify_bundle(bundle).ok
    assert "✓ Audit chain verified" in ro.post(f"{base}/audit/").content.decode()
    checks = ro.get(f"{base}/elections/{w['election'].id}/checks/").content.decode()
    assert "All checks passed" in checks
    # Records of voting and counting never name a candidate: who voted, never how.
    voting = [e for e in bundle["events"]
              if e["action"].split(".")[0] in ("ballot", "tally", "count")
              or e["action"] == "election.ballot_box_sealed"]
    assert {e["action"] for e in voting} >= {"ballot.accepted", "tally.completed"}
    text = json.dumps(voting, ensure_ascii=False)
    assert not any(c in text for c in ids + names)

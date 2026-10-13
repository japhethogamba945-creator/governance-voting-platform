import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction

from audit.models import AuditEvent
from members.importing import ImportFormatError, build_plan, parse_csv, summarise
from members.models import ImportBatch, Member
from platform_core.db import acting_as, db_context
from tests.factories import make_member, make_user
from tests.helpers import login

pytestmark = pytest.mark.django_db

HEADER = "member_number,full_name,email,phone,category,subscription_status,joined_on\n"


def _csv(*rows: str) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


@pytest.mark.parametrize("raw,message", [
    (b"", "empty"),
    (b"member_number,full_name\nA,B\n", "Missing column"),
    ((HEADER.strip() + ",shoe_size\n").encode(), "Unknown column"),
    (HEADER.encode(), "no member rows"),
    ("member_number,full_name,category,subscription_status,joined_on\nA-1,Zoë,full,paid,2020-01-01\n"
     .encode("latin-1"), "UTF-8"),
    (b"x" * (2 * 1024 * 1024 + 1), "larger than 2 MB"),
    (_csv("A-1,Too,many,values,full,paid,2020-01-01,extra"), "more values"),
])
def test_parse_rejects_malformed_files(raw, message):
    with pytest.raises(ImportFormatError, match=message):
        parse_csv(raw)


def test_parse_accepts_bom_and_optional_columns():
    raw = "﻿member_number,full_name,category,subscription_status,joined_on\n" \
          "A-1,Zoë Wambui (fictional),Full,paid,2020-01-01\n"
    rows = parse_csv(raw.encode("utf-8"))
    assert rows[0]["full_name"] == "Zoë Wambui (fictional)" and rows[0]["email"] == ""


def _plan(org, raw):
    with transaction.atomic(), db_context(org_id=org.id):
        plan = build_plan(org, parse_csv(raw))
    return {p.member_number + f"@{p.line}": p for p in plan}, summarise(plan)


def test_plan_classifies_rows(two_orgs):
    a, b = two_orgs
    make_member(a.org, "A-1", email="one@example.org")
    make_member(a.org, "A-2", email="two@example.org")
    make_member(b.org, "B-1", email="new@example.org")  # other org: does not conflict
    plan, counts = _plan(a.org, _csv(
        "A-1,Wanjiru Test (fictional),one@example.org,,full,paid,2020-01-15",   # unchanged
        "A-2,Wanjiru Test (fictional),two@example.org,,life,paid,2020-01-15",   # update
        "A-3,New Person (fictional),new@example.org,,full,unpaid,2024-05-01",   # create
        "A-4,Bad Date (fictional),,,full,paid,15/01/2020",                      # error
        "A-5,Bad Category (fictional),,,platinum,paid,2020-01-01",              # error
        "A-3,Repeat (fictional),,,full,paid,2020-01-01",                        # error: repeat
        "A-6,Email Taken (fictional),one@example.org,,full,paid,2020-01-01",    # error: email
    ))
    assert counts == {"create": 1, "update": 1, "unchanged": 1, "error": 4}
    assert plan["A-2@3"].changes == {"category": ["full", "life"]}
    assert "repeats line 4" in plan["A-3@7"].errors[0]
    assert "email already belongs to member A-1" in plan["A-6@8"].errors
    assert any("joined_on" in e for e in plan["A-4@5"].errors)


def _upload(client, org, raw):
    upload = SimpleUploadedFile("members.csv", raw, content_type="text/csv")
    return client.post(f"/o/{org.slug}/members/import/", {"file": upload})


def test_import_end_to_end(client, two_orgs):
    a, _ = two_orgs
    make_member(a.org, "A-1")
    inactive = make_member(a.org, "A-2", status="inactive",
                           deactivated_at="2024-01-01T00:00:00Z")
    login(client, a.admin, a.admin_device)
    r = _upload(client, a.org, _csv(
        "A-1,Wanjiru Test (fictional),,,full,exempt,2020-01-15",
        "A-2,Wanjiru Test (fictional),,,associate,paid,2020-01-15",
        "A-3,New Person (fictional),new@example.org,+254 711 000000,full,paid,2024-05-01",
    ))
    assert r.status_code == 302
    preview = client.get(r.url)
    assert b"inactive member" in preview.content and b"Apply import" in preview.content
    with acting_as(None):
        assert Member.all_objects.filter(organisation=a.org).count() == 2  # nothing yet
    batch_id = r.url.rstrip("/").split("/")[-1]
    r = client.post(f"/o/{a.org.slug}/members/import/{batch_id}/apply/")
    assert r.status_code == 302 and r.url.endswith("/members/")
    with acting_as(None):
        members = {m.member_number: m for m in Member.all_objects.filter(organisation=a.org)}
        assert members["A-1"].subscription_status == "exempt"
        assert members["A-2"].category == "associate" and members["A-2"].status == "inactive"
        assert members["A-3"].email == "new@example.org"
        assert inactive.id == members["A-2"].id
        batch_event = AuditEvent.objects.get(action="member.import_applied", organisation=a.org)
        assert batch_event.details["counts"] == {"create": 1, "update": 2, "unchanged": 0,
                                                 "error": 0}
        assert AuditEvent.objects.filter(action="member.updated", organisation=a.org).count() == 2
        assert ImportBatch.all_objects.get(pk=batch_id).rows == []  # personal data cleared
    # Applying twice is refused.
    r = client.post(f"/o/{a.org.slug}/members/import/{batch_id}/apply/", follow=True)
    assert b"already been applied" in r.content


def test_errors_block_apply(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    r = _upload(client, a.org, _csv("A-1,Fine (fictional),,,full,paid,2020-01-01",
                                    "A-2,Broken (fictional),,,full,maybe,2020-01-01"))
    preview = client.get(r.url)
    assert b"Apply import" not in preview.content and b"Fix the errors" in preview.content
    batch_id = r.url.rstrip("/").split("/")[-1]
    r = client.post(f"/o/{a.org.slug}/members/import/{batch_id}/apply/", follow=True)
    assert b"Fix the errors" in r.content
    with acting_as(None):
        assert not Member.all_objects.filter(organisation=a.org).exists()


def test_register_change_after_preview_blocks_apply(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    r = _upload(client, a.org, _csv("A-1,New Person (fictional),,,full,paid,2020-01-01"))
    make_member(a.org, "A-1", full_name="Created Meanwhile (fictional)")
    batch_id = r.url.rstrip("/").split("/")[-1]
    assert b"has changed since" in client.get(r.url).content
    r = client.post(f"/o/{a.org.slug}/members/import/{batch_id}/apply/", follow=True)
    assert b"changed since this preview" in r.content
    with acting_as(None):
        assert Member.all_objects.get(organisation=a.org).full_name == \
            "Created Meanwhile (fictional)"


def test_import_requires_manage_permission(client, two_orgs):
    a, _ = two_orgs
    from tenancy.roles import Role
    from tests.factories import add_member
    user, device = make_user("ro@example.org")
    add_member(a.org, user, Role.RETURNING_OFFICER)
    login(client, user, device)
    assert client.get(f"/o/{a.org.slug}/members/import/").status_code == 403


def test_discard(client, two_orgs):
    a, _ = two_orgs
    login(client, a.admin, a.admin_device)
    r = _upload(client, a.org, _csv("A-1,New Person (fictional),,,full,paid,2020-01-01"))
    batch_id = r.url.rstrip("/").split("/")[-1]
    client.post(f"/o/{a.org.slug}/members/import/{batch_id}/discard/")
    with acting_as(None):
        batch = ImportBatch.all_objects.get(pk=batch_id)
        assert batch.status == "discarded" and batch.rows == []

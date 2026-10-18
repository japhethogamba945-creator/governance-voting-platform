"""E2E seed: one fictional organisation with officials and members, all with known
authenticator keys. Run with: python manage.py shell < e2e/seed.py
Prints one line starting with E2E= followed by JSON. TEST DATA ONLY."""

import json
import uuid

from platform_core.db import acting_as
from tenancy.models import Organisation
from tenancy.roles import Role
from tests.factories import (
    KEYHOLDER_PASSPHRASE,
    PASSWORD,
    add_member,
    make_keyholder,
    make_member,
    make_user,
)

tag = uuid.uuid4().hex[:6]
with acting_as(None):
    org = Organisation.objects.create(slug=f"e2e-{tag}",
                                      legal_name=f"Mobile Test Club {tag} (fictional)")


def person(key, role, name, member_number=None):
    user, device = make_user(f"{key}-{tag}@example.org")
    with acting_as(None):
        user.full_name = name
        user.save(update_fields=["full_name"])
    add_member(org, user, role)
    if member_number:
        make_member(org, member_number, email=user.email, user=user, full_name=name)
    if role in (Role.ORG_ADMIN, Role.RETURNING_OFFICER, Role.AUDITOR):
        make_keyholder(org, user)
    return {"email": user.email, "password": PASSWORD, "totp_key": device.key, "name": name,
            "id": str(user.id), "member_number": member_number}


people = {
    "admin": person("admin", Role.ORG_ADMIN, "Achieng Admin (fictional)"),
    "ro": person("ro", Role.RETURNING_OFFICER, "Rashid Officer (fictional)"),
    "auditor": person("auditor", Role.AUDITOR, "Aisha Auditor (fictional)"),
    "alice": person("alice", Role.MEMBER, "Alice Candidate (fictional)", "E2E-1"),
    "bob": person("bob", Role.MEMBER, "Bob Candidate (fictional)", "E2E-2"),
    "carol": person("carol", Role.MEMBER, "Carol Voter (fictional)", "E2E-3"),
}
print("E2E=" + json.dumps({"org": org.slug, "passphrase": KEYHOLDER_PASSPHRASE,
                           "people": people}))

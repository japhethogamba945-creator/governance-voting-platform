"""Load a fictional demo organisation to click through the prototype (development only).

    python manage.py load_demo            # then: python manage.py demo_codes

Creates "Demo Club (fictional)" with an administrator, a Returning Officer, an
Auditor and five members, all with passwords and authenticator (TOTP) devices,
and three elections at different stages:

* Secretary 2026: nominations open (try nominating yourself as a member);
* Chair 2026: voting open (vote as a member, see the marked register as the RO);
* Treasurer 2026: voting closed with three ballots (count it with two keyholders,
  certify as the RO, publish as the administrator).

Refuses to run unless DEBUG is on. Every account uses an @demo.example address.
Run with the administrative login (as for migrate): it moves election times into
the past, which only a database superuser may do.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from audit.chaining import chain_and_anchor
from elections import lifecycle
from elections import services as election_services
from elections.models import Election
from elections.rules import default_rules
from electorate.models import Entitlement
from electorate.services import close_due_elections, open_due_elections
from keys import services as key_services
from members.importing import categories_for
from members.models import Member
from nominations import services as nomination_services
from nominations.models import Nomination
from platform_core.db import acting_as, db_context
from tenancy.models import Organisation, OrgMembership, RoleAssignment
from tenancy.roles import Role
from voting import services as voting_services

DEMO_DOMAIN = "demo.example"
# Published on purpose (demo only, refused unless DEBUG is on).
PASSWORD = "demo password 2026"  # noqa: S105  # nosec B105
KEYHOLDER_PASSPHRASE = "demo keyholder passphrase 2026"  # noqa: S105  # nosec B105
PEOPLE = [  # key, name, organisation role, member number (None = official only)
    ("admin", "Achieng Admin", Role.ORG_ADMIN, None),
    ("ro", "Rashid Officer", Role.RETURNING_OFFICER, None),
    ("auditor", "Aisha Auditor", Role.AUDITOR, None),
    ("alice", "Alice Mwangi", Role.MEMBER, "D-001"),
    ("bob", "Bob Otieno", Role.MEMBER, "D-002"),
    ("carol", "Carol Wanjiku", Role.MEMBER, "D-003"),
    ("dan", "Dan Kiprop", Role.MEMBER, "D-004"),
    ("esther", "Esther Njeri", Role.MEMBER, "D-005"),
]


class Command(BaseCommand):
    help = "Load a fictional demo organisation with elections at several stages (DEBUG only)."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="demo-club", help="Organisation slug.")

    def handle(self, *args, slug: str, **opts):
        if not settings.DEBUG:
            raise CommandError("load_demo only runs with DEBUG on (development). It creates "
                               "accounts with published passwords.")
        with acting_as(None):
            if Organisation.objects.filter(slug=slug).exists():
                raise CommandError(f"Organisation {slug!r} already exists. Use --slug.")
            org = Organisation.objects.create(slug=slug, legal_name="Demo Club (fictional)")
        tag = "" if slug == "demo-club" else f"+{slug}"
        people = {key: self._person(org, key, name, role, number, tag)
                  for key, name, role, number in PEOPLE}
        users = {key: p["user"] for key, p in people.items()}
        members = {key: p["member"] for key, p in people.items() if p["member"]}

        self._election(org, users, members, "Secretary 2026 (fictional)", stage="nomination",
                       candidates=[])
        self._election(org, users, members, "Chair 2026 (fictional)", stage="open",
                       candidates=["alice", "bob"])
        self._election(org, users, members, "Treasurer 2026 (fictional)", stage="closed",
                       candidates=["carol", "dan"],
                       votes={"alice": "carol", "bob": "carol", "esther": "dan"})
        chain_and_anchor()
        self._report(org, people)

    # --- people -------------------------------------------------------------------------

    def _person(self, org, key, name, role, number, tag):
        email = f"{key}{tag}@{DEMO_DOMAIN}"
        with acting_as(None):
            user = get_user_model().objects.create_user(email, password=PASSWORD,
                                                        status="active", full_name=name)
            TOTPDevice.objects.create(user=user, name="Demo authenticator", confirmed=True,
                                      key=secrets.token_hex(20))
            membership = OrgMembership.all_objects.create(organisation=org, user=user)
            RoleAssignment.all_objects.create(organisation=org, membership=membership, role=role)
            member = None
            if number:
                member = Member.all_objects.create(
                    organisation=org, member_number=number, full_name=name, email=email,
                    user=user, category="full", subscription_status="paid",
                    joined_on=timezone.localdate() - timedelta(days=3 * 365))
        if role in (Role.ORG_ADMIN, Role.RETURNING_OFFICER, Role.AUDITOR):
            with transaction.atomic(), db_context(org_id=org.id, user_id=user.id):
                key_services.set_passphrase(user, KEYHOLDER_PASSPHRASE)
        return {"user": user, "member": member, "role": role, "name": name}

    # --- elections -------------------------------------------------------------------------

    def _election(self, org, users, members, title, *, stage, candidates, votes=None):
        admin, ro = users["admin"], users["ro"]
        now = timezone.now()
        with transaction.atomic(), db_context(org_id=org.id, user_id=admin.id):
            election = election_services.create_election(
                Election(title=title, seats=1, opens_at=now + timedelta(hours=2),
                         closes_at=now + timedelta(days=7)), actor=admin)
            rules = default_rules(1, categories_for(org))
            rules["tie_procedure"]["description"] = (
                "Lots are drawn by the Returning Officer at the annual general meeting, "
                "before two witnesses.")
            election_services.save_rules(election, rules, actor=admin)
            election_services.transition(election, lifecycle.NOMINATION, actor=admin,
                                         roles={Role.ORG_ADMIN})
        if stage == "nomination":
            return election
        with transaction.atomic(), db_context(org_id=org.id, user_id=admin.id):
            election = Election.objects.get(pk=election.pk)
            for key in candidates:
                nomination_services.submit(
                    election, submitter=members[key], nominee=members[key], proposer=None,
                    seconder=None, statement=f"{members[key].full_name}: I will serve the "
                                             "club with care. (fictional)", actor=users[key])
        with transaction.atomic(), db_context(org_id=org.id, user_id=ro.id):
            election = Election.objects.select_related("current_rule_version").get(
                pk=election.pk)
            election_services.transition(election, lifecycle.REVIEW, actor=ro,
                                         roles={Role.RETURNING_OFFICER})
            for nomination in Nomination.objects.filter(election=election):
                nomination_services.decide(nomination, approve=True, reason="", actor=ro)
            election_services.transition(election, lifecycle.READY, actor=ro,
                                         roles={Role.RETURNING_OFFICER})
            election_services.transition(
                election, lifecycle.SCHEDULED, actor=ro, roles={Role.RETURNING_OFFICER},
                context={"keyholder_user_ids": [str(users["admin"].id),
                                                str(users["auditor"].id)]})
        self._move_time(election, "opens_at")
        open_due_elections()
        for voter, choice in (votes or {}).items():
            self._vote(org, election, users[voter], members[voter], members[choice])
        if stage == "closed":
            self._move_time(election, "closes_at")
            close_due_elections()
        return election

    def _move_time(self, election, column):
        """Stand in for waiting until the opening or closing time (superuser only)."""
        with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role = replica")
            if column == "opens_at":
                cursor.execute("UPDATE election SET opens_at = now() - interval '1 minute' "
                               "WHERE id = %s", [election.pk])
            else:
                cursor.execute("UPDATE election SET opens_at = least(opens_at, now() - "
                               "interval '2 minutes'), closes_at = now() - interval "
                               "'1 minute' WHERE id = %s", [election.pk])
            cursor.execute("SET LOCAL session_replication_role = origin")

    def _vote(self, org, election, user, member, choice):
        from nominations.models import Candidate

        with transaction.atomic(), db_context(org_id=org.id, user_id=user.id):
            candidate = Candidate.objects.get(election=election, nomination__nominee=choice)
            entitlement = Entitlement.objects.get(election=election, member=member)
            voting_services.cast(
                election_id=election.id, member_id=member.id, selections=[str(candidate.id)],
                idempotency_key=voting_services.issue_idempotency_key(entitlement.id),
                receipt_commitment=None, actor=user)

    # --- what to tell the person ---------------------------------------------------------------

    def _report(self, org, people):
        site = (settings.SITE_URL or "http://localhost:8000").rstrip("/")
        self.stdout.write(self.style.SUCCESS(f"\nDemo loaded: {org.legal_name} "
                                             f"({site}/o/{org.slug}/)\n"))
        self.stdout.write("Everyone signs in with the password and a 6-digit code:")
        self.stdout.write(f"  password:  {PASSWORD}")
        self.stdout.write(f"  codes:     python manage.py demo_codes --slug {org.slug}\n")
        for p in people.values():
            self.stdout.write(f"  {p['user'].email:<32} {Role(p['role']).label}")
        self.stdout.write(f"\nKeyholder passphrase (admin, RO, auditor): {KEYHOLDER_PASSPHRASE}")
        self.stdout.write(
            "\nTry:\n"
            "  * Secretary 2026: sign in as carol and nominate yourself.\n"
            "  * Chair 2026: sign in as carol, dan or esther and vote; as ro, see who voted.\n"
            "  * Treasurer 2026 (voting closed): as ro, Counting -> Start the count; then as\n"
            "    admin and as auditor, provide your share with the passphrase; as ro, certify;\n"
            "    as admin, publish; as any member, see the results and check a receipt.\n"
            "  Run `python manage.py run_scheduler` after certifying to chain the audit trail.\n")

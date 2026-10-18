"""Independent re-checks of one election's recorded fingerprints (auditor view)."""

import hashlib
import json
from dataclasses import dataclass

from django.db import connection


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool | None  # None = not applicable yet
    detail: str


def _sql_one(sql: str, params: list):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def election_checks(election) -> list[Check]:
    from electorate.models import ElectorateEntry, Entitlement
    from electorate.services import electorate_digest
    from tally import services as tally_services
    from tally.models import Tally

    checks: list[Check] = []
    version = election.current_rule_version
    if version is not None:
        recomputed = _sql_one("SELECT encode(sha256(convert_to(rules::text, 'UTF8')), 'hex') "
                              "FROM election_rule_version WHERE id = %s", [version.id])[0]
        checks.append(Check(f"Rules version {version.version} fingerprint",
                            recomputed == version.rules_sha256, version.rules_sha256))
    if election.public_key is not None:
        fpr = hashlib.sha256(bytes(election.public_key)).hexdigest()
        checks.append(Check("Counting key fingerprint", fpr == election.public_key_fpr,
                            election.public_key_fpr))
    if election.electorate_digest:
        entries = list(ElectorateEntry.objects.filter(election=election))
        amended = sum(1 for e in entries if e.amendment_reason)
        if amended:
            checks.append(Check("Electorate fingerprint", None,
                                f"{amended} entries were amended while suspended; the "
                                "fingerprint covers the electorate frozen at opening."))
        else:
            checks.append(Check("Electorate fingerprint (recomputed from the frozen list)",
                                electorate_digest(entries) == election.electorate_digest,
                                f"{len(entries)} voters · {election.electorate_digest}"))
    if election.ballot_set_digest:
        count, digest = _sql_one("SELECT ballot_count, digest FROM ballot_set_summary(%s)",
                                 [election.id])
        consumed = Entitlement.objects.filter(election=election, status="consumed").count()
        checks.append(Check("Ballot box unchanged since it was sealed",
                            digest == election.ballot_set_digest,
                            f"{count} ballots · {election.ballot_set_digest}"))
        checks.append(Check("Ballots equal members marked as having voted", count == consumed,
                            f"{count} ballots, {consumed} marked"))
    for tally in Tally.objects.filter(election=election).order_by("version"):
        checks.append(Check(f"Count version {tally.version} reconciliation",
                            tally.reconciliation_ok and tally.ballot_set_digest
                            == election.ballot_set_digest,
                            ", ".join(f"{k.replace('_', ' ')}: {'yes' if v else 'NO'}"
                                      for k, v in tally.reconciliation.items())))
    for certification in election.certifications.order_by("certified_at"):
        document = json.loads(certification.document)
        label = "Certified result" + (" (superseded)" if certification.superseded_at else "")
        checks.append(Check(f"{label}: document matches its fingerprint",
                            hashlib.sha256(certification.document.encode()).hexdigest()
                            == certification.content_sha256, certification.content_sha256))
        checks.append(Check(f"{label}: refers to this ballot box",
                            document["election"]["ballot_set_digest"]
                            == election.ballot_set_digest, ""))
        receipts = tally_services.receipt_commitments(election)
        checks.append(Check(f"{label}: published receipt list unchanged",
                            document["receipts"]["sha256"]
                            == tally_services.receipts_digest(receipts),
                            f"{len(receipts)} receipts"))
    return checks

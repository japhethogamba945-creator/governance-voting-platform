"""Keyholder keys, the key ceremony (at "Approve and schedule") and key reconstruction."""

import base64
import hashlib
import uuid

from django.db.models import Q
from django.utils import timezone

from audit.services import record as audit
from elections import lifecycle
from elections.services import ElectionError, register_transition_hook
from platform_core.db import current_org_id
from tenancy.models import RoleAssignment
from tenancy.roles import OFFICIAL_ROLES

from . import crypto
from .models import KeyholderKey, KeyShare

KEYHOLDERS_REQUIRED = 3  # plus the sealed offline backup: any 2 of these 4 can count


class KeysError(ValueError):
    pass


def active_key(user) -> KeyholderKey | None:
    return KeyholderKey.objects.filter(user=user, retired_at__isnull=True).first()


def is_official(user) -> bool:
    now = timezone.now()
    return RoleAssignment.objects.filter(
        membership__user=user, role__in=[r.value for r in OFFICIAL_ROLES],
        election_id__isnull=True, valid_from__lte=now,
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now)).exists()


def set_passphrase(user, passphrase: str, *, current_passphrase: str | None = None) -> None:
    """Create the official's keyholder key, or re-protect it under a new passphrase.

    Changing the passphrase keeps the same key pair (existing shares stay
    usable), so the current passphrase is required.
    """
    if not is_official(user):
        raise KeysError("Only officials can hold election keys.")
    existing = active_key(user)
    try:
        if existing is None:
            material = crypto.new_keyholder_keypair(passphrase)
            KeyholderKey.objects.create(organisation_id=current_org_id(), user=user,
                                        public_key=material["public_key"],
                                        salt=material["salt"],
                                        encrypted_private_key=material["encrypted_private_key"],
                                        kdf=material["kdf"])
            action = "keyholder.key_created"
        else:
            private = crypto.unlock_keyholder(bytes(existing.salt),
                                              bytes(existing.encrypted_private_key),
                                              current_passphrase or "")
            material = crypto.reprotect_keyholder(private, passphrase)
            KeyholderKey.objects.filter(pk=existing.pk).update(
                salt=material["salt"], encrypted_private_key=material["encrypted_private_key"],
                kdf=material["kdf"])
            action = "keyholder.passphrase_changed"
    except crypto.KeyError_ as exc:
        raise KeysError(str(exc)) from exc
    audit(action, actor=user, organisation_id=current_org_id(), object_type="user",
          object_id=user.id)


def eligible_keyholders():
    """Officials of the current organisation who have set a keyholder passphrase."""
    return (KeyholderKey.objects.filter(retired_at__isnull=True)
            .select_related("user").order_by("user__email"))


@register_transition_hook
def ceremony_hook(election, target: str, actor, context: dict) -> None:
    """Run the key ceremony as part of "Approve and schedule"."""
    if target != lifecycle.SCHEDULED:
        return
    chosen = [actor.id, *[uuid.UUID(str(u)) for u in context.get("keyholder_user_ids", [])]]
    if len(chosen) != KEYHOLDERS_REQUIRED or len(set(chosen)) != KEYHOLDERS_REQUIRED:
        raise ElectionError(f"Choose {KEYHOLDERS_REQUIRED - 1} other officials as keyholders "
                            "(different from you and from each other).")
    keys = {k.user_id: k for k in eligible_keyholders().filter(user_id__in=chosen)}
    missing = [uid for uid in chosen if uid not in keys]
    if missing:
        raise ElectionError("Every keyholder must be an official who has set a keyholder "
                            "passphrase.")
    for uid in chosen:
        if not is_official(keys[uid].user):
            raise ElectionError("Every keyholder must currently hold an official role.")
    result = crypto.ceremony([bytes(keys[uid].public_key) for uid in chosen])
    for index, uid in enumerate(chosen, start=1):
        bundle = result["sealed_bundles"][index - 1]
        KeyShare.objects.create(
            organisation_id=election.organisation_id, election=election, share_index=index,
            keyholder_key=keys[uid], holder_label=keys[uid].user.email,
            recipient_public_key=bytes(keys[uid].public_key), encrypted_bundle=bundle,
            bundle_sha256=hashlib.sha256(bundle).hexdigest())
    KeyShare.objects.create(
        organisation_id=election.organisation_id, election=election,
        share_index=KEYHOLDERS_REQUIRED + 1, keyholder_key=None,
        holder_label="Sealed offline backup", recipient_public_key=result["backup_public_key"],
        encrypted_bundle=result["backup_bundle"],
        bundle_sha256=hashlib.sha256(result["backup_bundle"]).hexdigest())
    from elections.models import Election  # local: avoid an import cycle at app loading

    Election.objects.filter(pk=election.pk).update(public_key=result["public_key"],
                                                   public_key_fpr=result["fingerprint"])
    election.public_key, election.public_key_fpr = result["public_key"], result["fingerprint"]
    context["backup_secret"] = result["backup_secret"]  # shown once, never stored
    audit("election.key_ceremony", actor=actor, organisation_id=election.organisation_id,
          object_type="election", object_id=election.id,
          details={"public_key_fpr": result["fingerprint"],
                   "keyholders": [str(uid) for uid in chosen],
                   "shares": KEYHOLDERS_REQUIRED + 1, "threshold": 2})


def _open_share(contribution: dict, shares: list[KeyShare]):
    """Open the contributor's own share: (share, bundle, private key for staged pieces)."""
    if "backup_code" in contribution:
        share = next((s for s in shares if s.keyholder_key_id is None), None)
        if share is None:
            raise KeysError("This election has no backup share.")
        bundle = crypto.open_backup_bundle(bytes(share.encrypted_bundle),
                                           contribution["backup_code"])
        private = crypto.backup_private_key(crypto.parse_backup_secret(
            contribution["backup_code"]))
        return share, bundle, private
    share = next((s for s in shares if s.keyholder_key_id
                  and s.keyholder_key.user_id == contribution["user"].id), None)
    if share is None:
        raise KeysError("You are not a keyholder for this election.")
    key = share.keyholder_key
    private = crypto.unlock_keyholder(bytes(key.salt), bytes(key.encrypted_private_key),
                                      contribution["passphrase"])
    return share, crypto.open_holder_bundle(bytes(share.encrypted_bundle), private), private


def _shares(election) -> list[KeyShare]:
    return list(KeyShare.objects.filter(election=election).select_related("keyholder_key"))


def stage_share(election, contribution: dict) -> tuple[int, dict[str, str]]:
    """First of two contributions to a count: returns (share index, staged pieces).

    ``contribution`` is ``{"user": <user>, "passphrase": str}`` or
    ``{"backup_code": str}``. The staged pieces are sealed to the other holders'
    public keys; the server cannot rebuild the key from them alone.
    """
    shares = _shares(election)
    try:
        share, bundle, _ = _open_share(contribution, shares)
        recipients = {}
        for s in shares:
            if s.share_index == share.share_index:
                continue
            public = s.recipient_public_key or (s.keyholder_key.public_key if s.keyholder_key_id
                                                else None)
            if public is not None:
                recipients[s.share_index] = bytes(public)
        return share.share_index, crypto.stage_pieces(share.share_index, bundle, recipients)
    except crypto.KeyError_ as exc:
        raise KeysError(str(exc)) from exc


def share_index_for(election, contribution: dict) -> int | None:
    """The share index a contribution would use, without opening anything."""
    for s in _shares(election):
        if "backup_code" in contribution and s.keyholder_key_id is None:
            return s.share_index
        if "user" in contribution and s.keyholder_key_id \
                and s.keyholder_key.user_id == contribution["user"].id:
            return s.share_index
    return None


def rebuild_from_staged(election, contribution: dict, staged: dict[int, dict]):
    """Second contribution: rebuild the election key from a staged first contribution.

    ``staged`` maps the first contributor's share index to its staged pieces.
    Returns ``(first index, own index, nacl PrivateKey)``; the key must stay in
    memory only.
    """
    shares = _shares(election)
    try:
        share, bundle, private = _open_share(contribution, shares)
        for first_index, pieces in staged.items():
            if first_index == share.share_index:
                continue
            sealed = pieces.get(str(share.share_index))
            if sealed is None:
                continue
            key = crypto.rebuild_from_staged(bundle, private, base64.b64decode(sealed),
                                             bytes(election.public_key))
            return first_index, share.share_index, key
    except crypto.KeyError_ as exc:
        raise KeysError(str(exc)) from exc
    raise KeysError("The other contribution cannot be combined with yours. Ask the Returning "
                    "Officer to restart the count.")


def rebuild_election_key(election, contributions: list[dict]):
    """Rebuild the election's private key from two contributions.

    Each contribution is ``{"user": <user>, "passphrase": str}`` for a keyholder,
    or ``{"backup_code": str}`` for the sealed backup. Returns a nacl PrivateKey;
    callers must keep it in memory only (increment 6.7 counting).
    """
    if len(contributions) != 2:
        raise KeysError("Exactly two keyholders are needed.")
    bundles = []
    shares = {s.share_index: s for s in KeyShare.objects.filter(election=election)
              .select_related("keyholder_key")}
    try:
        for contribution in contributions:
            if "backup_code" in contribution:
                backup = next((s for s in shares.values() if s.keyholder_key_id is None), None)
                if backup is None:
                    raise KeysError("This election has no backup share.")
                bundles.append(crypto.open_backup_bundle(bytes(backup.encrypted_bundle),
                                                         contribution["backup_code"]))
                continue
            share = next((s for s in shares.values() if s.keyholder_key_id
                          and s.keyholder_key.user_id == contribution["user"].id), None)
            if share is None:
                raise KeysError("That person is not a keyholder for this election.")
            key = share.keyholder_key
            private = crypto.unlock_keyholder(bytes(key.salt), bytes(key.encrypted_private_key),
                                              contribution["passphrase"])
            bundles.append(crypto.open_holder_bundle(bytes(share.encrypted_bundle), private))
        return crypto.rebuild_private_key(bundles[0], bundles[1], bytes(election.public_key))
    except crypto.KeyError_ as exc:
        raise KeysError(str(exc)) from exc

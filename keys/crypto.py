"""Election key generation, 2-of-n replicated secret sharing and reconstruction.

Design: docs/design/05-security-review.md §3, with the custody variation in
docs/design/DECISIONS.md I6.5.1.

* The election key is an X25519 key pair (libsodium). Ballots are sealed to the
  public key (increment 6.6); the private key is only ever reassembled in memory
  to count (increment 6.7).
* Replicated sharing, threshold 2: for every pair of holders {i, j} a fresh
  random 32-byte value r is drawn; holder i receives r and holder j receives
  sk XOR r. Any two holders combine the pieces for the pair they share; any one
  holder's pieces are independent uniform random bytes and reveal nothing.
* Each holder's pieces are encrypted to that holder's own keyholder public key
  (sealed box). The backup holder's pieces are encrypted under a random 256-bit
  backup secret that is shown once, to be printed and sealed offline.

Python cannot reliably wipe memory; key material is kept in local variables only
and never logged or written unencrypted (residual risk R3).
"""

import base64
import hashlib
import itertools
import json
import secrets

import nacl.exceptions
import nacl.pwhash
import nacl.secret
import nacl.utils
from django.conf import settings
from nacl.public import PrivateKey, PublicKey, SealedBox

KEY_BYTES = 32
MIN_PASSPHRASE_LENGTH = 20
_KDF_LIMITS = {
    "moderate": (nacl.pwhash.argon2id.OPSLIMIT_MODERATE, nacl.pwhash.argon2id.MEMLIMIT_MODERATE),
    "interactive": (nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE,
                    nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE),
}


class KeyError_(ValueError):
    """Wrong passphrase, missing shares or inconsistent key material."""


def fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


# --- keyholder key pairs (one per official per organisation) ---------------------

def _kdf(passphrase: str, salt: bytes) -> bytes:
    ops, mem = _KDF_LIMITS[getattr(settings, "KEYHOLDER_KDF", "moderate")]
    return nacl.pwhash.argon2id.kdf(nacl.secret.SecretBox.KEY_SIZE, passphrase.encode(),
                                    salt, opslimit=ops, memlimit=mem)


def new_keyholder_keypair(passphrase: str) -> dict:
    """A key pair whose private half is encrypted under the holder's passphrase."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise KeyError_(f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters.")
    private = PrivateKey.generate()
    salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    box = nacl.secret.SecretBox(_kdf(passphrase, salt))
    return {"public_key": bytes(private.public_key), "salt": salt,
            "encrypted_private_key": bytes(box.encrypt(bytes(private))),
            "kdf": getattr(settings, "KEYHOLDER_KDF", "moderate")}


def reprotect_keyholder(private: PrivateKey, passphrase: str) -> dict:
    """Encrypt an existing keyholder private key under a new passphrase."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise KeyError_(f"Use a passphrase of at least {MIN_PASSPHRASE_LENGTH} characters.")
    salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
    box = nacl.secret.SecretBox(_kdf(passphrase, salt))
    return {"salt": salt, "encrypted_private_key": bytes(box.encrypt(bytes(private))),
            "kdf": getattr(settings, "KEYHOLDER_KDF", "moderate")}


def unlock_keyholder(salt: bytes, encrypted_private_key: bytes, passphrase: str) -> PrivateKey:
    try:
        raw = nacl.secret.SecretBox(_kdf(passphrase, salt)).decrypt(encrypted_private_key)
    except nacl.exceptions.CryptoError as exc:
        raise KeyError_("That passphrase is not correct.") from exc
    return PrivateKey(raw)


# --- election key and shares -----------------------------------------------------------

def split(secret: bytes, holders: int) -> list[list[tuple[int, int, str]]]:
    """2-of-``holders`` replicated sharing. Returns, per holder, (i, j, piece_b64) items."""
    bundles: list[list[tuple[int, int, str]]] = [[] for _ in range(holders)]
    for i, j in itertools.combinations(range(holders), 2):
        r = secrets.token_bytes(KEY_BYTES)
        bundles[i].append((i, j, base64.b64encode(r).decode()))
        bundles[j].append((i, j, base64.b64encode(_xor(secret, r)).decode()))
    return bundles


def combine(bundle_a: list, bundle_b: list) -> bytes:
    """Rebuild the secret from two holders' bundles."""
    pieces_a = {(i, j): base64.b64decode(p) for i, j, p in bundle_a}
    pieces_b = {(i, j): base64.b64decode(p) for i, j, p in bundle_b}
    shared = set(pieces_a) & set(pieces_b)
    if len(shared) != 1:
        raise KeyError_("These two shares do not belong to the same key.")
    pair = shared.pop()
    return _xor(pieces_a[pair], pieces_b[pair])


def ceremony(holder_public_keys: list[bytes]) -> dict:
    """Generate an election key and its shares for the given holders plus a backup.

    Returns the public key, its fingerprint, one sealed bundle per holder, the
    backup bundle and the backup secret (to be shown once). Before returning,
    the key is rebuilt from every pair of bundles and checked against the
    public key (self-test).
    """
    election_key = PrivateKey.generate()
    secret = bytes(election_key)
    public = bytes(election_key.public_key)
    bundles = split(secret, len(holder_public_keys) + 1)  # last holder = backup
    for a, b in itertools.combinations(bundles, 2):
        if bytes(PrivateKey(combine(a, b)).public_key) != public:  # pragma: no cover
            raise KeyError_("Key ceremony self-test failed.")
    sealed = [SealedBox(PublicKey(pk)).encrypt(json.dumps(bundle).encode())
              for pk, bundle in zip(holder_public_keys, bundles[:-1], strict=True)]
    backup_secret = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
    backup = nacl.secret.SecretBox(backup_secret).encrypt(json.dumps(bundles[-1]).encode())
    del secret, election_key, bundles
    return {"public_key": public, "fingerprint": fingerprint(public),
            "sealed_bundles": [bytes(s) for s in sealed], "backup_bundle": bytes(backup),
            "backup_secret": format_backup_secret(backup_secret)}


def format_backup_secret(raw: bytes) -> str:
    text = base64.b32encode(raw).decode().rstrip("=")
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def parse_backup_secret(text: str) -> bytes:
    cleaned = "".join(text.split()).replace("-", "").upper()
    try:
        raw = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))
    except (ValueError, TypeError) as exc:
        raise KeyError_("That backup code is not valid.") from exc
    if len(raw) != nacl.secret.SecretBox.KEY_SIZE:
        raise KeyError_("That backup code is not valid.")
    return raw


def open_holder_bundle(sealed: bytes, holder_private_key: PrivateKey) -> list:
    try:
        return json.loads(SealedBox(holder_private_key).decrypt(sealed))
    except nacl.exceptions.CryptoError as exc:
        raise KeyError_("This share was not made for that keyholder.") from exc


def open_backup_bundle(encrypted: bytes, backup_code: str) -> list:
    try:
        return json.loads(nacl.secret.SecretBox(parse_backup_secret(backup_code))
                          .decrypt(encrypted))
    except nacl.exceptions.CryptoError as exc:
        raise KeyError_("That backup code does not open this election's backup share.") from exc


def rebuild_private_key(bundle_a: list, bundle_b: list, expected_public_key: bytes) -> PrivateKey:
    key = PrivateKey(combine(bundle_a, bundle_b))
    if bytes(key.public_key) != expected_public_key:
        raise KeyError_("The rebuilt key does not match the election's public key.")
    return key

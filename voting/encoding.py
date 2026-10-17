"""Ballot plaintext format, sealing and receipt codes (docs/design/05-security-review.md §4).

Plaintext: 2-byte big-endian length, then canonical JSON
``{"e": "<election id>", "s": [<sorted candidate ids>], "v": 1}``, zero-padded
to 4096 bytes, so the ciphertext length never reveals how many candidates were
chosen. The election id inside stops a ballot being counted in another election.
Sealed with libsodium ``crypto_box_seal`` to the election's public key:
4096 + 48 = 4144 bytes.
"""

import hashlib
import json
import secrets
import uuid

from nacl.public import PrivateKey, PublicKey, SealedBox

PLAINTEXT_BYTES = 4096
CIPHERTEXT_BYTES = PLAINTEXT_BYTES + 48  # sealed box: ephemeral public key (32) + MAC (16)
RECEIPT_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
RECEIPT_LENGTH = 16  # 80 random bits


def encode(election_id: uuid.UUID, candidate_ids: list[uuid.UUID]) -> bytes:
    body = json.dumps({"e": str(election_id), "s": sorted(str(c) for c in candidate_ids),
                       "v": 1}, sort_keys=True, separators=(",", ":")).encode()
    if len(body) > PLAINTEXT_BYTES - 2:
        raise ValueError("ballot too large")
    return len(body).to_bytes(2, "big") + body + b"\0" * (PLAINTEXT_BYTES - 2 - len(body))


def decode(plaintext: bytes) -> dict:
    if len(plaintext) != PLAINTEXT_BYTES:
        raise ValueError("wrong plaintext size")
    size = int.from_bytes(plaintext[:2], "big")
    if size > PLAINTEXT_BYTES - 2 or any(plaintext[2 + size:]):
        raise ValueError("bad padding")
    return json.loads(plaintext[2:2 + size])


def seal(public_key: bytes, plaintext: bytes) -> bytes:
    ciphertext = bytes(SealedBox(PublicKey(public_key)).encrypt(plaintext))
    if len(ciphertext) != CIPHERTEXT_BYTES:  # pragma: no cover - libsodium invariant
        raise ValueError("unexpected ciphertext size")
    return ciphertext


def unseal(private_key: PrivateKey, ciphertext: bytes) -> bytes:
    return SealedBox(private_key).decrypt(ciphertext)


def new_receipt_code() -> str:
    return "".join(secrets.choice(RECEIPT_ALPHABET) for _ in range(RECEIPT_LENGTH))


def normalise_receipt_code(code: str) -> str:
    return "".join(code.split()).replace("-", "").upper()


def format_receipt_code(code: str) -> str:
    code = normalise_receipt_code(code)
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def receipt_commitment(election_id: uuid.UUID, code: str) -> bytes:
    """SHA-256("rcpt/v1" + election id + code). Computed in the browser too (ballot.js)."""
    return hashlib.sha256(f"rcpt/v1{election_id}{normalise_receipt_code(code)}".encode()).digest()


def ballot_set_digest(ciphertexts) -> str:
    """Order-independent digest of a set of ballots (same as SQL ballot_set_summary)."""
    hashes = sorted(hashlib.sha256(bytes(c)).hexdigest() for c in ciphertexts)
    return hashlib.sha256("\n".join(hashes).encode()).hexdigest()

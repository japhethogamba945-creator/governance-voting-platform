"""Rejects passwords that appear in a breached-password corpus (ASVS 2.1.7, finding F-2).

The check is offline and private: it reads a local mirror stored as one file per
five-character SHA-1 prefix (``5BAA6.txt`` holding ``SUFFIX:COUNT`` lines), the
k-anonymity "range" format of the Have I Been Pwned Pwned Passwords service. Only
the file for the password's prefix is read; the password never leaves the server.
See docs/operations/breached-passwords.md for building the mirror.
"""

import hashlib
import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

log = logging.getLogger(__name__)


class BreachedPasswordValidator:
    def __init__(self, directory: str | None = None):
        self.directory = directory

    def _directory(self) -> str:
        return self.directory or getattr(settings, "BREACHED_PASSWORDS_DIR", "")

    def is_breached(self, password: str) -> bool:
        directory = self._directory()
        if not directory:
            return False
        digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        try:
            with open(Path(directory) / f"{prefix}.txt", encoding="ascii") as handle:
                for line in handle:
                    if line[:35].upper() == suffix:
                        return True
        except FileNotFoundError:
            return False  # a partial mirror: this prefix is not listed
        except OSError:
            log.exception("Breached-password mirror unreadable")
            return False
        return False

    def validate(self, password: str, user=None) -> None:
        if self.is_breached(password):
            raise ValidationError(
                "This password has appeared in a data breach elsewhere, so it is easy for "
                "attackers to guess. Choose a different one.",
                code="password_breached")

    def get_help_text(self) -> str:
        return "Your password must not be one that has appeared in a known data breach."

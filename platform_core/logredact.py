"""Redact bearer tokens that appear in URLs before they reach any log.

Invitation links carry a single-use token in the path (/invite/<token>/). The
view moves it into the session at once, but web-server access logs and Django's
request logging would otherwise record it, letting anyone with log access set
up an invited member's account first.
"""

import logging
import re

_INVITE = re.compile(r"(/invite/)(?!accept/)[^/\s?#\"']+")


def redact(text: str) -> str:
    return _INVITE.sub(r"\1[redacted]", text)


class RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


try:  # gunicorn is a production dependency; keep this module importable without it
    from gunicorn.glogging import Logger as _GunicornLogger
except ImportError:  # pragma: no cover
    _GunicornLogger = None

if _GunicornLogger is not None:

    class GunicornLogger(_GunicornLogger):
        """Access log with tokens redacted from every atom (request line, path, referer)."""

        def atoms(self, resp, req, environ, request_time):
            atoms = super().atoms(resp, req, environ, request_time)
            return {k: redact(v) if isinstance(v, str) else v for k, v in atoms.items()}

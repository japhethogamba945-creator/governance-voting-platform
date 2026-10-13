import logging
from datetime import timedelta

import pytest
from gunicorn.config import Config

from platform_core.logredact import GunicornLogger, RedactSecretsFilter, redact


@pytest.mark.parametrize("raw,expected", [
    ('"GET /invite/AbC-123_xyz/ HTTP/1.1" 302', '"GET /invite/[redacted]/ HTTP/1.1" 302'),
    ("Gone: /invite/AbC-123_xyz/", "Gone: /invite/[redacted]/"),
    ("GET /invite/accept/ HTTP/1.1", "GET /invite/accept/ HTTP/1.1"),
    ("GET /o/club/members/ HTTP/1.1", "GET /o/club/members/ HTTP/1.1"),
])
def test_redact(raw, expected):
    assert redact(raw) == expected


def test_filter_rewrites_formatted_message():
    record = logging.LogRecord("django.server", logging.INFO, __file__, 1,
                               '"%s" %s', ("GET /invite/secret-token/ HTTP/1.1", 302), None)
    RedactSecretsFilter().filter(record)
    assert "secret-token" not in record.getMessage()


def test_logging_config_redacts_on_every_handler(settings):
    for handler in settings.LOGGING["handlers"].values():
        assert "redact" in handler.get("filters", [])
    for name in ("django", "django.server"):
        assert settings.LOGGING["loggers"][name]["handlers"] == ["console"]
        assert settings.LOGGING["loggers"][name]["propagate"] is False


def test_gunicorn_access_log_redacts():
    logger = GunicornLogger(Config())

    class Req:
        method, path, query, version, uri = "GET", "/invite/tok123/", "", (1, 1), "/invite/tok123/"
        headers: list = []

    class Resp:
        status, response_length, sent, headers = "302 Found", 0, 0, []

    environ = {"REQUEST_METHOD": "GET", "RAW_URI": "/invite/tok123/",
               "PATH_INFO": "/invite/tok123/", "QUERY_STRING": "",
               "SERVER_PROTOCOL": "HTTP/1.1", "REMOTE_ADDR": "203.0.113.1"}
    atoms = logger.atoms(Resp(), Req(), environ, timedelta(seconds=0.01))
    values = " ".join(v for v in atoms.values() if isinstance(v, str))
    assert "/invite/[redacted]/" in values and "tok123" not in values

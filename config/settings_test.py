"""Test settings.

Tests log in with an administrative (superuser) role so the test runner can
create the test database and run migrations. The ``default`` connection then
switches to ``app_rw`` (see ``tests/conftest.py``), so every request in the test
suite runs under the same least-privilege role and Row-Level Security as
production.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("DB_USER", os.environ.get("TEST_DB_ADMIN_USER", "gvp_admin"))
os.environ.setdefault("DB_PASSWORD", os.environ.get("TEST_DB_ADMIN_PASSWORD", "gvp_admin"))
os.environ.setdefault("SECURE_SSL_REDIRECT", "false")

from .settings import *  # noqa: E402,F403

PASSWORD_HASHERS = ["tests.hashers.FastArgon2Hasher"]
ALLOWED_HOSTS = ["testserver", "localhost"]

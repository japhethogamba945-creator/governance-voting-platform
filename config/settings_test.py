"""Test settings.

Tests log in with an administrative (superuser) role so the test runner can
create the test database and run migrations. The ``default`` connection then
switches to ``app_rw`` (see ``tests/conftest.py``), so every request in the test
suite runs under the same least-privilege role and Row-Level Security as
production.
"""

import os

# Tests must migrate as the admin login; a role left over in the shell would break that.
for _var in ("DB_ASSUME_ROLE", "PLATFORM_DB_ASSUME_ROLE"):
    os.environ.pop(_var, None)

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key-not-for-production")
os.environ.setdefault("DB_USER", os.environ.get("TEST_DB_ADMIN_USER", "gvp_admin"))
os.environ.setdefault("DB_PASSWORD", os.environ.get("TEST_DB_ADMIN_PASSWORD", "gvp_admin"))
os.environ.setdefault("SECURE_SSL_REDIRECT", "false")

from .settings import *  # noqa: E402,F403

PASSWORD_HASHERS = ["tests.hashers.FastArgon2Hasher"]
KEYHOLDER_KDF = "interactive"  # cheap Argon2id parameters for the test suite only
ALLOWED_HOSTS = ["testserver", "localhost"]
SITE_URL = "https://vote.example"

import tempfile  # noqa: E402

PRIVATE_MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="gvp-test-media-"))  # noqa: F405
STORAGES = {**STORAGES, "default": {  # noqa: F405
    "BACKEND": "django.core.files.storage.FileSystemStorage",
    "OPTIONS": {"location": str(PRIVATE_MEDIA_ROOT), "base_url": None}}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
AUDIT_ANCHOR_DIR = tempfile.mkdtemp(prefix="gvp-test-anchors-")

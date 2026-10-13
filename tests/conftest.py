import pytest
from django.db import connections


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """After migrating as the admin login, run everything else as ``app_rw``."""
    with django_db_blocker.unblock():
        conn = connections["default"]
        conn.close()
        conn.settings_dict["OPTIONS"]["assume_role"] = "app_rw"
    yield
    with django_db_blocker.unblock():
        conn.close()
        conn.settings_dict["OPTIONS"].pop("assume_role", None)


@pytest.fixture
def two_orgs(db):
    from tests.factories import make_org_with_admin

    return make_org_with_admin("acacia-club", "Acacia Gardens Club (fictional)"), \
        make_org_with_admin("baobab-club", "Baobab Rowing Club (fictional)")

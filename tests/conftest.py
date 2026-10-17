import pytest
from django.db import connections


@pytest.fixture(scope="session")
def django_db_setup(request, django_test_environment, django_db_blocker):
    """Create the test database (always; pytest-django would otherwise create it only
    when a collected test is marked ``django_db``, and the concurrency tests manage
    their own transactions), migrate as the admin login, then run everything else
    as ``app_rw``."""
    from django.test.utils import setup_databases, teardown_databases

    with django_db_blocker.unblock():
        db_cfg = setup_databases(verbosity=request.config.option.verbose, interactive=False,
                                 aliases={"default"}, serialized_aliases=set())
        conn = connections["default"]
        conn.close()
        conn.settings_dict["OPTIONS"]["assume_role"] = "app_rw"
    yield
    with django_db_blocker.unblock():
        conn.close()
        conn.settings_dict["OPTIONS"].pop("assume_role", None)
        teardown_databases(db_cfg, verbosity=request.config.option.verbose)


@pytest.fixture
def two_orgs(db):
    from tests.factories import make_org_with_admin

    return make_org_with_admin("acacia-club", "Acacia Gardens Club (fictional)"), \
        make_org_with_admin("baobab-club", "Baobab Rowing Club (fictional)")

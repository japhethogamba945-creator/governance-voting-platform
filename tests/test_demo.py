"""The development demo: load_demo and demo_codes."""

import io
import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from elections.models import Election
from platform_core.db import acting_as
from voting.models import Ballot

pytestmark = pytest.mark.django_db


def test_demo_refuses_without_debug(settings):
    settings.DEBUG = False
    for command in ("load_demo", "demo_codes"):
        with pytest.raises(CommandError, match="DEBUG"):
            call_command(command, stdout=io.StringIO())


def test_demo_loads_elections_at_three_stages_and_its_accounts_work(settings):
    settings.DEBUG = True
    out = io.StringIO()
    call_command("load_demo", slug="demo-test", stdout=out)
    assert "Demo loaded" in out.getvalue()
    with acting_as(None):
        states = dict(Election.all_objects.filter(organisation__slug="demo-test")
                      .values_list("title", "state"))
        treasurer = Election.all_objects.get(organisation__slug="demo-test",
                                             title__startswith="Treasurer")
        assert Ballot.objects.filter(election=treasurer).count() == 3
    assert states == {"Secretary 2026 (fictional)": "nomination",
                      "Chair 2026 (fictional)": "open",
                      "Treasurer 2026 (fictional)": "closed"}
    codes = io.StringIO()
    call_command("demo_codes", slug="demo-test", stdout=codes)
    ro_code = re.search(r"ro\+demo-test@demo\.example\s+(\d{6})", codes.getvalue()).group(1)
    client = Client()
    client.post("/accounts/login/", {"email": "ro+demo-test@demo.example",
                                     "password": "demo password 2026"})
    assert client.post("/accounts/login/verify/", {"token": ro_code}).status_code == 302
    page = client.get(f"/o/demo-test/elections/{treasurer.id}/count/").content.decode()
    assert "Start the count" in page

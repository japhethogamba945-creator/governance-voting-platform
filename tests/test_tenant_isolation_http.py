"""Acceptance test 1 (HTTP layer): every organisation-scoped URL is checked automatically.

New ``/o/<org_slug>/...`` routes are picked up without editing this file. Every
other URL parameter must have a factory in ``OBJECT_FACTORIES`` (creating an
object in a given organisation), or the test fails, so no route goes untested.

For each route the test checks that a member of organisation A:
* gets a non-404 for A's own objects (control),
* gets 404 for organisation B's URL (cross-tenant),
* gets 404 for A's URL pointing at B's objects (object-level IDOR),
with identical 404 bodies, for both GET and POST.
"""

import re
import uuid

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from tenancy.roles import Role
from tests.factories import (
    add_member,
    make_document,
    make_election,
    make_import_batch,
    make_meeting,
    make_member,
    make_nomination,
    make_user,
)
from tests.helpers import login, without_csrf

pytestmark = pytest.mark.django_db

OBJECT_FACTORIES = {
    "member_id": lambda fx: make_member(fx.org, f"F-{uuid.uuid4().hex[:8]}").id,
    "batch_id": lambda fx: make_import_batch(fx.org, fx.admin).id,
    "meeting_id": lambda fx: make_meeting(fx.org, fx.admin).id,
    "election_id": lambda fx: make_election(fx.org, fx.admin).id,
    "target": lambda fx: "nomination",  # a plain value, identical for both organisations
    "nomination_id": lambda fx: make_nomination(fx.org, fx.admin).id,
    "document_id": lambda fx: make_document(fx.org, fx.admin).id,
}


def _org_routes():
    routes = []

    def walk(patterns, prefix_ns, prefix_route):
        for p in patterns:
            route = prefix_route + str(p.pattern)
            if isinstance(p, URLResolver):
                ns = p.namespace
                walk(p.url_patterns, f"{prefix_ns}{ns}:" if ns else prefix_ns, route)
            elif isinstance(p, URLPattern) and "<slug:org_slug>" in route and p.name:
                routes.append((f"{prefix_ns}{p.name}", route))

    walk(get_resolver().url_patterns, "", "")
    return routes


ORG_ROUTES = _org_routes()


def test_routes_discovered():
    names = {name for name, _ in ORG_ROUTES}
    assert {"tenancy:dashboard", "tenancy:settings"} <= names


def _params(route):
    return [p for p in re.findall(r"<(?:\w+:)?(\w+)>", route) if p != "org_slug"]


def test_every_route_parameter_has_a_factory():
    for name, route in ORG_ROUTES:
        missing = [p for p in _params(route) if p not in OBJECT_FACTORIES]
        assert not missing, f"{name}: add OBJECT_FACTORIES for {missing}"


def _objects(route, fx):
    return {p: OBJECT_FACTORIES[p](fx) for p in _params(route)}


def _everything_in(org):
    """A user holding every role in ``org``, with a register entry, so that each
    control request passes permission checks and exercises the real lookup."""
    user, device = make_user(f"all-roles-{uuid.uuid4().hex[:6]}@example.org")
    add_member(org, user, *Role)
    make_member(org, f"ALL-{uuid.uuid4().hex[:6]}", email=user.email, user=user)
    return user, device


@pytest.mark.parametrize("name,route", ORG_ROUTES)
def test_member_of_a_gets_404_for_b(client, two_orgs, name, route):
    a, b = two_orgs
    a_objs, b_objs = _objects(route, a), _objects(route, b)
    login(client, *_everything_in(a.org))  # also satisfies step-up for the next 5 minutes
    own = client.get(reverse(name, kwargs={"org_slug": a.org.slug, **a_objs}))
    assert own.status_code != 404, f"{name}: control request failed"
    for method in (client.get, client.post):
        missing = method(reverse(name, kwargs={"org_slug": "no-such-org", **b_objs}))
        assert missing.status_code == 404
        other_org = method(reverse(name, kwargs={"org_slug": b.org.slug, **b_objs}))
        assert other_org.status_code == 404, f"{name}: cross-tenant access not blocked"
        assert without_csrf(other_org.content) == without_csrf(missing.content), \
            "response leaks organisation existence"
        if b_objs:
            # Same organisation URL, other organisation's object. A method the
            # view does not accept is refused (405) before any lookup; otherwise 404.
            own_status = method(reverse(name, kwargs={"org_slug": a.org.slug,
                                                      **_objects(route, a)})).status_code
            idor = method(reverse(name, kwargs={"org_slug": a.org.slug, **b_objs}))
            expected = 405 if own_status == 405 else 404
            assert idor.status_code == expected, f"{name}: B's object reachable via A's URL"


@pytest.mark.parametrize("name,route", ORG_ROUTES)
def test_anonymous_is_redirected_to_login(client, two_orgs, name, route):
    a, _ = two_orgs
    response = client.get(reverse(name, kwargs={"org_slug": a.org.slug, **_objects(route, a)}))
    assert response.status_code == 302
    assert response.url.startswith("/accounts/login/")

"""Acceptance test 1 (HTTP layer): every organisation-scoped URL is checked automatically.

New ``/o/<org_slug>/...`` routes are picked up without editing this file. A
route with extra URL parameters must be given sample values in
``EXTRA_KWARGS``, or the test fails, so no route goes untested.
"""

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from tests.helpers import login, without_csrf

pytestmark = pytest.mark.django_db

EXTRA_KWARGS: dict[str, dict] = {}


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


def _url(name, slug):
    kwargs = {"org_slug": slug, **EXTRA_KWARGS.get(name, {})}
    return reverse(name, kwargs=kwargs)


@pytest.mark.parametrize("name,route", ORG_ROUTES)
def test_member_of_a_gets_404_for_b(client, two_orgs, name, route):
    a, b = two_orgs
    login(client, a.admin, a.admin_device)
    own = client.get(_url(name, a.org.slug))
    assert own.status_code != 404, f"{name}: control request failed"
    for method in (client.get, client.post):
        other = method(_url(name, b.org.slug))
        missing = method(_url(name, "no-such-org"))
        assert other.status_code == 404, f"{name}: cross-tenant access not blocked"
        assert missing.status_code == 404
        assert without_csrf(other.content) == without_csrf(missing.content), \
            "response leaks organisation existence"


@pytest.mark.parametrize("name,route", ORG_ROUTES)
def test_anonymous_is_redirected_to_login(client, two_orgs, name, route):
    a, _ = two_orgs
    response = client.get(_url(name, a.org.slug))
    assert response.status_code == 302
    assert response.url.startswith("/accounts/login/")

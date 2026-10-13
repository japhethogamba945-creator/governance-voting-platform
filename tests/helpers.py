import re

from django.test import Client

from tests.factories import PASSWORD, totp_now


def login(client: Client, user, device, password: str = PASSWORD):
    """Full password + TOTP login. Returns the final response."""
    r = client.post("/accounts/login/", {"email": user.email, "password": password})
    assert r.status_code == 302, r.content
    assert r.url == "/accounts/login/verify/"
    r = client.post("/accounts/login/verify/", {"token": totp_now(device)})
    assert r.status_code == 302, r.content
    return r


_CSRF = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]+"')


def without_csrf(content: bytes) -> bytes:
    """Strip per-response CSRF tokens so two pages can be compared byte for byte."""
    return _CSRF.sub(b'name="csrfmiddlewaretoken" value=""', content)

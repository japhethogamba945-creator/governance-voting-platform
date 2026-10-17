from django.urls import include, path

from members.public_views import invite_accept, invite_start
from platform_core.views import healthz
from tenancy.views import home

urlpatterns = [
    path("", home, name="home"),
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("accounts.urls")),
    path("invite/accept/", invite_accept, name="invite_accept"),
    path("invite/<str:token>/", invite_start, name="invite_start"),
    path("o/<slug:org_slug>/", include("tenancy.urls")),
    path("o/<slug:org_slug>/members/", include("members.urls")),
    path("o/<slug:org_slug>/elections/", include("elections.urls")),
    path("o/<slug:org_slug>/", include("nominations.urls")),
    path("o/<slug:org_slug>/", include("keys.urls")),
    path("o/<slug:org_slug>/", include("voting.urls")),
    path("o/<slug:org_slug>/", include("tally.urls")),
]

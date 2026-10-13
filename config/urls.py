from django.urls import include, path

from platform_core.views import healthz
from tenancy.views import home

urlpatterns = [
    path("", home, name="home"),
    path("healthz", healthz, name="healthz"),
    path("accounts/", include("accounts.urls")),
    path("o/<slug:org_slug>/", include("tenancy.urls")),
]

from django.urls import path

from . import views

app_name = "tenancy"

urlpatterns = [
    path("", views.org_dashboard, name="dashboard"),
    path("settings/", views.org_settings, name="settings"),
    path("roles/", views.roles_list, name="roles"),
    path("roles/grant/", views.role_grant, name="role_grant"),
    path("roles/revoke/", views.role_revoke, name="role_revoke"),
    path("roles/invite/", views.official_invite, name="official_invite"),
]

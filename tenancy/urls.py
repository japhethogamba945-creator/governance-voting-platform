from django.urls import path

from . import views

app_name = "tenancy"

urlpatterns = [
    path("", views.org_dashboard, name="dashboard"),
    path("settings/", views.org_settings, name="settings"),
]

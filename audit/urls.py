from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("audit/", views.trail, name="trail"),
    path("audit/bundle.json", views.bundle, name="bundle"),
    path("elections/<uuid:election_id>/checks/", views.election_checks, name="checks"),
]

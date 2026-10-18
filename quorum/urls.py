from django.urls import path

from . import views

app_name = "quorum"

urlpatterns = [
    path("elections/<uuid:election_id>/attendance/", views.attendance, name="attendance"),
    path("elections/<uuid:election_id>/attendance/add/", views.add, name="add"),
    path("elections/<uuid:election_id>/attendance/remove/", views.remove, name="remove"),
]

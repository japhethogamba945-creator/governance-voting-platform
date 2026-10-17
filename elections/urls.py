from django.urls import path

from . import views

app_name = "elections"

urlpatterns = [
    path("", views.election_list, name="list"),
    path("new/", views.election_create, name="create"),
    path("meetings/", views.meeting_list, name="meeting_list"),
    path("meetings/new/", views.meeting_create, name="meeting_create"),
    path("meetings/<uuid:meeting_id>/edit/", views.meeting_edit, name="meeting_edit"),
    path("<uuid:election_id>/", views.election_detail, name="detail"),
    path("<uuid:election_id>/edit/", views.election_edit, name="edit"),
    path("<uuid:election_id>/rules/", views.election_rules, name="rules"),
    path("<uuid:election_id>/transition/<slug:target>/", views.election_transition,
         name="transition"),
]

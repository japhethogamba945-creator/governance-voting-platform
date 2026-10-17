from django.urls import path

from . import views

app_name = "tally"

urlpatterns = [
    path("elections/<uuid:election_id>/count/", views.count_page, name="count"),
    path("elections/<uuid:election_id>/count/start/", views.start, name="start"),
    path("elections/<uuid:election_id>/count/cancel/", views.cancel, name="cancel"),
    path("elections/<uuid:election_id>/count/contribute/", views.contribute,
         name="contribute"),
    path("elections/<uuid:election_id>/count/tie/", views.tie, name="tie"),
    path("elections/<uuid:election_id>/certify/", views.certify, name="certify"),
    path("elections/<uuid:election_id>/publish/", views.publish, name="publish"),
    path("elections/<uuid:election_id>/results/", views.results, name="results"),
    path("elections/<uuid:election_id>/results/document.json", views.result_document,
         name="document"),
    path("elections/<uuid:election_id>/receipts/", views.receipts, name="receipts"),
]

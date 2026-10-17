from django.urls import path

from . import views

app_name = "voting"

urlpatterns = [
    path("elections/<uuid:election_id>/ballot/", views.ballot, name="ballot"),
    path("elections/<uuid:election_id>/ballot/review/", views.review, name="review"),
    path("elections/<uuid:election_id>/ballot/cast/", views.cast, name="cast"),
    path("elections/<uuid:election_id>/marked-register/", views.marked_register,
         name="marked_register"),
]

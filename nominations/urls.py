from django.urls import path

from . import views

app_name = "nominations"

urlpatterns = [
    path("elections/<uuid:election_id>/nominations/mine/", views.my_nominations, name="mine"),
    path("elections/<uuid:election_id>/nominations/new/", views.nominate, name="nominate"),
    path("elections/<uuid:election_id>/nominations/", views.review_list, name="review"),
    path("nominations/<uuid:nomination_id>/confirm/", views.confirm, name="confirm"),
    path("nominations/<uuid:nomination_id>/decline/", views.decline, name="decline"),
    path("nominations/<uuid:nomination_id>/withdraw/", views.withdraw, name="withdraw"),
    path("nominations/<uuid:nomination_id>/statement/", views.edit_statement,
         name="statement"),
    path("nominations/<uuid:nomination_id>/documents/", views.add_document, name="add_document"),
    path("nominations/<uuid:nomination_id>/decide/", views.decide, name="decide"),
    path("nomination-documents/<uuid:document_id>/", views.download_document,
         name="download_document"),
]

from django.urls import path

from . import views

app_name = "members"

urlpatterns = [
    path("", views.member_list, name="list"),
    path("new/", views.member_create, name="create"),
    path("import/", views.import_upload, name="import_upload"),
    path("import/<uuid:batch_id>/", views.import_preview, name="import_preview"),
    path("import/<uuid:batch_id>/apply/", views.import_apply, name="import_apply"),
    path("import/<uuid:batch_id>/discard/", views.import_discard, name="import_discard"),
    path("<uuid:member_id>/", views.member_detail, name="detail"),
    path("<uuid:member_id>/edit/", views.member_edit, name="edit"),
    path("<uuid:member_id>/invite/", views.member_invite, name="invite"),
    path("<uuid:member_id>/deactivate/", views.member_deactivate, name="deactivate"),
    path("<uuid:member_id>/reactivate/", views.member_reactivate, name="reactivate"),
]

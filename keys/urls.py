from django.urls import path

from . import views

app_name = "keys"

urlpatterns = [
    path("keyholder/", views.keyholder_passphrase, name="passphrase"),
]

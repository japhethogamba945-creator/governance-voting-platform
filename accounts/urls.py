from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("login/verify/", views.login_verify, name="login_verify"),
    path("mfa/setup/", views.mfa_setup, name="mfa_setup"),
    path("mfa/recovery-codes/", views.recovery_codes, name="recovery_codes"),
    path("step-up/", views.step_up, name="step_up"),
    path("logout/", views.logout_view, name="logout"),
]

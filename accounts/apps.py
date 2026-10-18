from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (registers the deployment checks)

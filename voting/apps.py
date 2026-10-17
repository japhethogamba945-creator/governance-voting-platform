from django.apps import AppConfig


class VotingConfig(AppConfig):
    name = "voting"

    def ready(self) -> None:
        from . import services  # noqa: F401  (registers the closing hook)

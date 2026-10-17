from django.apps import AppConfig


class TallyConfig(AppConfig):
    name = "tally"

    def ready(self) -> None:
        from . import services  # noqa: F401  (registers the result checks)

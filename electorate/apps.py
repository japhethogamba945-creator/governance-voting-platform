from django.apps import AppConfig


class ElectorateConfig(AppConfig):
    name = "electorate"

    def ready(self) -> None:
        from . import services  # noqa: F401  (registers the election transition hook)

from django.apps import AppConfig


class NominationsConfig(AppConfig):
    name = "nominations"

    def ready(self) -> None:
        from . import services  # noqa: F401  (registers the election transition hook)

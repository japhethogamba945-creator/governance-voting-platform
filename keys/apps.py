from django.apps import AppConfig


class KeysConfig(AppConfig):
    name = "keys"

    def ready(self) -> None:
        from . import services  # noqa: F401  (registers the key ceremony hook)

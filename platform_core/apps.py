from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.db.models.signals import post_migrate


def _apply_grants(sender, using="default", **kwargs):
    from .grants import apply_grants, missing_roles

    missing = missing_roles(using)
    if missing:
        raise ImproperlyConfigured(
            f"Database roles missing: {', '.join(missing)}. Run db/roles.sql first."
        )
    apply_grants(using)


class PlatformCoreConfig(AppConfig):
    name = "platform_core"

    def ready(self) -> None:
        # Sent once per app; restricting the sender makes it run once per migrate.
        post_migrate.connect(_apply_grants, sender=self, dispatch_uid="platform_core.grants")

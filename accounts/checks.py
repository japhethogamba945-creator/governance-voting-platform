from django.conf import settings
from django.core.checks import Warning, register


@register(deploy=True)
def breached_passwords_configured(app_configs, **kwargs):
    """Deployment check (manage.py check --deploy): finding F-2 needs the offline mirror."""
    if settings.DEBUG or getattr(settings, "BREACHED_PASSWORDS_DIR", ""):
        return []
    return [Warning("BREACHED_PASSWORDS_DIR is not set, so new passwords are not checked "
                    "against known breaches.",
                    hint="See docs/operations/breached-passwords.md.", id="accounts.W001")]

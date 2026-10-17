from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from accounts.stepup import step_up_required
from tenancy.permissions import requires
from tenancy.roles import Perm

from . import services
from .forms import PassphraseForm


@sensitive_post_parameters("current_passphrase", "passphrase", "passphrase_again")
@require_http_methods(["GET", "POST"])
@requires(Perm.KEYS_HOLD)
@step_up_required
def keyholder_passphrase(request, org_slug):
    has_key = services.active_key(request.user) is not None
    form = PassphraseForm(request.POST or None, has_key=has_key)
    if request.method == "POST" and form.is_valid():
        try:
            services.set_passphrase(request.user, form.cleaned_data["passphrase"],
                                    current_passphrase=form.cleaned_data.get("current_passphrase"))
        except services.KeysError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Your keyholder passphrase is set. Keep it safe: it is "
                                      "needed to count votes and cannot be recovered.")
            return redirect("tenancy:dashboard", org_slug=org_slug)
    return render(request, "keys/passphrase.html", {"form": form, "has_key": has_key})

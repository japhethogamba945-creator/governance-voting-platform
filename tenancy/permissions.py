from functools import wraps

from django.core.exceptions import PermissionDenied

from .roles import Perm


def requires(perm: Perm):
    """Allow the view only if the user's roles in ``request.org`` grant ``perm``."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if perm not in getattr(request, "permissions", frozenset()):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        wrapper.required_permission = perm
        return wrapper

    return decorator

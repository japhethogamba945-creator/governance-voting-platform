"""Request-scoped database context for tenant isolation.

Two layers are kept in step:

* PostgreSQL settings ``app.org_id`` / ``app.user_id`` read by the Row-Level
  Security policies (``current_org_id()`` / ``current_app_user_id()``).
* Context variables read by :class:`TenantManager`, so ORM queries are scoped to
  the current organisation in the application as well.

Settings are applied with ``set_config(..., is_local => true)`` and therefore
only live for the current transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import connections, models

_current_org: ContextVar[uuid.UUID | None] = ContextVar("current_org_id", default=None)
_current_user: ContextVar[uuid.UUID | None] = ContextVar("current_user_id", default=None)


class NoTenantContext(RuntimeError):
    """A tenant-scoped query was attempted without an organisation context."""


def current_org_id() -> uuid.UUID | None:
    return _current_org.get()


def require_org_id() -> uuid.UUID:
    org_id = _current_org.get()
    if org_id is None:
        raise NoTenantContext("Tenant-scoped query outside an organisation context")
    return org_id


def _apply(using: str, user_id: uuid.UUID | None, org_id: uuid.UUID | None) -> None:
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.user_id', %s, true), set_config('app.org_id', %s, true)",
            ["" if user_id is None else str(user_id), "" if org_id is None else str(org_id)],
        )


@contextmanager
def db_context(
    *, user_id: uuid.UUID | None = None, org_id: uuid.UUID | None = None, using: str = "default"
) -> Iterator[None]:
    """Run a block with the given user/organisation context.

    Must be entered inside ``transaction.atomic(using=using)``. On exit the
    previous context is restored.
    """
    connection = connections[using]
    if not connection.in_atomic_block:
        raise RuntimeError("db_context() must be used inside transaction.atomic()")
    user_token = _current_user.set(user_id)
    org_token = _current_org.set(org_id)
    _apply(using, user_id, org_id)
    try:
        yield
    finally:
        _current_user.reset(user_token)
        _current_org.reset(org_token)
        # After an error the transaction is rolled back to the enclosing
        # savepoint, which also reverts the settings; only restore explicitly
        # when the connection is still usable.
        if not connection.needs_rollback:
            _apply(using, _current_user.get(), _current_org.get())


@contextmanager
def lookup_scope(*, email: str | None = None, invite_token_hash: str | None = None,
                 using: str = "default") -> Iterator[None]:
    """Make exactly one otherwise-invisible row readable for a pre-authentication lookup.

    ``app_user`` rows are visible by email only while ``app.auth_email`` is set
    (sign-in), and invitations by token hash only while
    ``app.invite_token_hash`` is set (accepting an invitation). Callers must
    already hold the email/token, so this reveals nothing they did not supply.
    """
    connection = connections[using]
    if not connection.in_atomic_block:
        raise RuntimeError("lookup_scope() must be used inside transaction.atomic()")

    def apply(email_value: str, token_value: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.auth_email', %s, true), "
                "set_config('app.invite_token_hash', %s, true)",
                [email_value, token_value],
            )

    apply((email or "").strip().lower(), invite_token_hash or "")
    try:
        yield
    finally:
        if not connection.needs_rollback:
            apply("", "")


@contextmanager
def scheduler_scope(using: str = "default") -> Iterator[None]:
    """Let the scheduler list elections that are due to open or close, across
    organisations (policy ``scheduler_due`` on ``election``). Only ids, states and
    times are read under this scope; each election is then handled in its own
    organisation's context."""
    connection = connections[using]
    if not connection.in_atomic_block:
        raise RuntimeError("scheduler_scope() must be used inside transaction.atomic()")
    with connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.scheduler', 'on', true)")
    try:
        yield
    finally:
        if not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.scheduler', '', true)")


@contextmanager
def acting_as(role: str | None, using: str = "default") -> Iterator[None]:
    """Temporarily switch the database role (``None`` = the login role).

    Only usable where the login role may ``SET ROLE`` (development, tests and
    management commands run by an operator). Production web processes log in
    directly as their least-privilege role, so this cannot widen their access.
    """
    connection = connections[using]
    assumed = connection.settings_dict.get("OPTIONS", {}).get("assume_role")
    with connection.cursor() as cursor:
        if role is None:
            cursor.execute("RESET ROLE")
        else:
            cursor.execute("SELECT set_config('role', %s, false)", [role])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            if assumed:
                cursor.execute("SELECT set_config('role', %s, false)", [assumed])
            else:
                cursor.execute("RESET ROLE")


class TenantManager(models.Manager):
    """Default manager for tenant-owned models: always filters by organisation."""

    def get_queryset(self) -> models.QuerySet:
        return super().get_queryset().filter(organisation_id=require_org_id())

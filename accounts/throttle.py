"""PostgreSQL-backed rate limiting for authentication attempts.

Account keys are derived from the submitted email whether or not an account
exists, so lockout behaviour cannot be used to discover accounts.
"""

import hashlib
import hmac
from dataclasses import dataclass

from django.conf import settings
from django.db import connection


@dataclass(frozen=True)
class Policy:
    threshold: int
    window_seconds: int
    exponential: bool  # True: lock 2^(n-threshold) minutes; False: lock until window end
    max_lock_seconds: int


ACCOUNT_POLICY = Policy(threshold=5, window_seconds=24 * 3600, exponential=True,
                        max_lock_seconds=3600)
IP_POLICY = Policy(threshold=50, window_seconds=600, exponential=False, max_lock_seconds=600)


def account_key(email: str) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), email.strip().lower().encode(),
                      hashlib.sha256).hexdigest()
    return f"acct:{digest[:40]}"


def ip_key(ip: str) -> str:
    return f"ip:{ip[:64]}"


def is_locked(*keys: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM accounts_throttle WHERE key = ANY(%s) AND locked_until > now() LIMIT 1",
            [list(keys)],
        )
        return cursor.fetchone() is not None


def record_failure(key: str, policy: Policy) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO accounts_throttle (key, failures, window_start, locked_until)
            VALUES (%(key)s, 1, now(), NULL)
            ON CONFLICT (key) DO UPDATE SET
                failures = CASE WHEN accounts_throttle.window_start
                                     < now() - make_interval(secs => %(window)s)
                                THEN 1 ELSE accounts_throttle.failures + 1 END,
                window_start = CASE WHEN accounts_throttle.window_start
                                         < now() - make_interval(secs => %(window)s)
                                    THEN now() ELSE accounts_throttle.window_start END
            RETURNING failures, window_start
            """,
            {"key": key, "window": policy.window_seconds},
        )
        failures, _ = cursor.fetchone()
        if failures < policy.threshold:
            return
        if policy.exponential:
            seconds = min(60 * 2 ** (failures - policy.threshold), policy.max_lock_seconds)
            cursor.execute(
                "UPDATE accounts_throttle SET locked_until = now() + make_interval(secs => %s) "
                "WHERE key = %s",
                [seconds, key],
            )
        else:
            cursor.execute(
                "UPDATE accounts_throttle SET locked_until = window_start "
                "+ make_interval(secs => %s) WHERE key = %s",
                [policy.window_seconds, key],
            )


def record_login_failure(email: str, ip: str) -> None:
    record_failure(account_key(email), ACCOUNT_POLICY)
    record_failure(ip_key(ip), IP_POLICY)


def reset(key: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM accounts_throttle WHERE key = %s", [key])

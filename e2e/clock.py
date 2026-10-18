"""E2E clock: move an election's opening or closing time into the past, standing in
for waiting hours in real time. Run with:
  E2E_ELECTION=<id> E2E_MOVE=open|close python manage.py shell < e2e/clock.py
TEST ONLY: bypasses the database triggers with the superuser-only replication role."""

import os

from django.db import connection, transaction

from platform_core.db import acting_as

column = {"open": "opens_at", "close": "closes_at"}[os.environ["E2E_MOVE"]]
with transaction.atomic(), acting_as(None), connection.cursor() as cursor:
    cursor.execute("SET LOCAL session_replication_role = replica")
    if column == "opens_at":
        cursor.execute("UPDATE election SET opens_at = now() - interval '1 minute' WHERE id = %s",
                       [os.environ["E2E_ELECTION"]])
    else:
        cursor.execute("UPDATE election SET opens_at = least(opens_at, now() - interval "
                       "'2 minutes'), closes_at = now() - interval '1 minute' WHERE id = %s",
                       [os.environ["E2E_ELECTION"]])
    cursor.execute("SET LOCAL session_replication_role = origin")
print(f"E2E clock: {column} moved into the past")

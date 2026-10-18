#!/usr/bin/env bash
# Acceptance test 17: run the full election lifecycle on emulated phones.
#   e2e/run.sh [chromium] [webkit]
# Uses the database configured by the DB_* variables (a dedicated, migrated test
# database), seeds a fresh fictional organisation for each browser, and serves the
# app with the development server. TEST DATA ONLY.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-.venv/bin/python}"
PORT="${E2E_PORT:-8765}"
export E2E_BASE_URL="http://localhost:${PORT}" PYTHON
export DJANGO_DEBUG="${DJANGO_DEBUG:-true}" SECURE_SSL_REDIRECT=false SECURE_COOKIES=false
export AUDIT_ANCHOR_DIR="${AUDIT_ANCHOR_DIR:-$(mktemp -d)}"
browsers=("${@:-chromium}")

"$PYTHON" manage.py runserver "$PORT" --noreload --insecure > e2e-server.log 2>&1 &
server=$!
trap 'kill $server 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -sf "http://localhost:${PORT}/healthz" >/dev/null && break; sleep 1; done

status=0
for browser in "${browsers[@]}"; do
  seed="$(mktemp)"
  "$PYTHON" manage.py shell < e2e/seed.py | sed -n 's/^E2E=//p' > "$seed"
  node e2e/lifecycle.mjs "$seed" "$browser" || status=1
done
exit $status

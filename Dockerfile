FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN DJANGO_SECRET_KEY=build-only uv run --no-sync python manage.py collectstatic --noinput \
    && useradd --system --uid 10001 app
USER app
EXPOSE 8000
# Process types: web (default), worker and scheduler arrive with background jobs.
# gunicorn.conf.py (token-redacting access log) is picked up from /app automatically.
CMD ["uv", "run", "--no-sync", "gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "3"]

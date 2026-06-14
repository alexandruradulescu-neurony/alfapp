# LORA — production container image.
#
# Builds a Django + Playwright + WeasyPrint image suitable for Railway,
# Render, Fly.io, or any container PaaS. ~1.2 GB final image (Chromium
# is most of that).
#
# Usage notes:
# - PORT is provided by the platform (Railway sets $PORT automatically).
# - DATABASE_URL points at the platform's managed Postgres add-on.
# - Migrations run on container start; safe across deploy retries because
#   Django migrations are idempotent.
# - This image serves WEB requests only (gunicorn). Scheduled work runs from a
#   SEPARATE Railway cron service using the same image with start command
#   `python manage.py run_scheduled_jobs` — never an in-process scheduler here
#   (multiple gunicorn workers/replicas would each fire it).

FROM python:3.13-slim AS base

# Don't write .pyc files, don't buffer stdout (so logs flush immediately)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ----- System dependencies -----
# - libpango/libcairo/libgdk-pixbuf/libffi/shared-mime-info: WeasyPrint PDF generation
# - fontconfig + fonts: WeasyPrint glyph rendering
# - curl/ca-certificates: Playwright browser download
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libcairo2 \
      libgdk-pixbuf-2.0-0 \
      libffi-dev \
      shared-mime-info \
      fontconfig \
      fonts-liberation \
      curl \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ----- Python dependencies (cached layer) -----
# Copy requirements first so Docker caches this layer separately from app code.
COPY requirements.txt .
RUN pip install -r requirements.txt

# ----- Playwright browsers + their system dependencies -----
# `--with-deps` installs Chromium's required system libs (libnss, libnspr,
# libatk, etc.) via apt-get. Heavy step (~400 MB), but only re-runs when
# the playwright version in requirements.txt changes.
RUN playwright install --with-deps chromium

# ----- Application code -----
COPY . .

# ----- Collect static files into STATIC_ROOT so WhiteNoise can serve them -----
# settings.py requires SECRET_KEY at import time, but real secrets are runtime
# env vars not present during `docker build`. Supply a THROWAWAY key for this
# one build step only (the real key comes from the Railway env var at runtime).
# DEBUG defaults to False and DATABASE_URL defaults to sqlite (collectstatic
# touches neither). No `|| true`: if static collection fails, fail the build
# loudly rather than shipping an unstyled site.
RUN SECRET_KEY=build-time-collectstatic-only-not-used-at-runtime \
    python manage.py collectstatic --noinput

# ----- Runtime -----
# Bind to $PORT (set by the platform). Migrations run first; if they fail,
# the container exits and the platform retries on rollout.
# 2 workers is plenty for a small internal tool; bump if you see latency.
# Timeout 120s for Playwright screenshot requests that can take a while.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn lora_app.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 120 --access-logfile - --error-logfile -"]

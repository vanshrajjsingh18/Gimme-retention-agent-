# Single-image build for Railway (and any other container host).
#
# The dashboard and the API ship together and are served from one origin.
# That is not just convenience: it removes the CORS configuration and the
# build-time API URL, which are the two things most likely to be wrong on a
# first deploy and which fail in ways that look like the app is broken.

# --- stage 1: build the dashboard -----------------------------------------
FROM node:22-slim AS dashboard

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# Same-origin: the bundle calls /api/v1/... relative to wherever it is served,
# so there is no host to bake in and nothing to change between environments.
ENV VITE_API_URL=""
RUN npm run build


# --- stage 2: the application ---------------------------------------------
FROM python:3.11-slim AS app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Every dependency resolves to a prebuilt wheel, psycopg2-binary included, so
# no compiler is needed — installing one and purging it again would only spend
# build minutes and add an apt step that can fail. libpq5 is still required at
# runtime: the psycopg2 wheel links against it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=dashboard /build/dist ./frontend/dist

# Written to by the CSV inbox and, when running on SQLite, the database.
RUN mkdir -p /app/data/inbox

ENV FRONTEND_DIST=/app/frontend/dist \
    INBOX_DIR=/app/data/inbox \
    ENVIRONMENT=production \
    DEBUG=false \
    PYTHONPATH=/app/backend

EXPOSE 8000

# Railway supplies $PORT; default to 8000 so the image also runs locally.
CMD ["sh", "-c", "cd /app/backend && exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

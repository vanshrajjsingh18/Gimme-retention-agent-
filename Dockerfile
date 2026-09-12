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

# psycopg2 needs libpq at runtime; the build-time headers go away with the
# layer, so the image stays small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt \
 && apt-get purge -y gcc libpq-dev && apt-get autoremove -y

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

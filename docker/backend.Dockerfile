# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a source change does not invalidate the install layer.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./

# The SQLite file and the ingestion inbox live on a mounted volume.
RUN mkdir -p /app/data/inbox

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# The app creates its schema and baseline configuration on startup, so no
# separate migration step is needed for a first run.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

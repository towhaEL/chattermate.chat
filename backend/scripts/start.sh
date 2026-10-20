#!/bin/bash
set -e

# Set environment variable to prevent tokenizer deadlock warnings
export TOKENIZERS_PARALLELISM=false

# Wait for PostgreSQL to be ready. Docker Compose exposes the database as
# `db`, while Render supplies a full DATABASE_URL for its managed Postgres.
# Resolve the host from DATABASE_URL so the same image works in both places.
DB_HOST="${DB_HOST:-}"
DB_PORT="${DB_PORT:-5432}"
if [ -z "$DB_HOST" ] && [ -n "${DATABASE_URL:-}" ]; then
  DB_HOST=$(python - "$DATABASE_URL" <<'PY'
from sys import argv
from urllib.parse import urlparse

url = urlparse(argv[1])
print(url.hostname or "")
PY
  )
  DB_PORT=$(python - "$DATABASE_URL" <<'PY'
from sys import argv
from urllib.parse import urlparse

print(urlparse(argv[1]).port or 5432)
PY
  )
fi

if [ -z "$DB_HOST" ]; then
  DB_HOST=db
fi

echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
while ! nc -z "$DB_HOST" "$DB_PORT"; do
  sleep 0.5
done
echo "PostgreSQL is ready!"

# Check if Firebase credentials exist, but don't create them if missing
if [ ! -z "$FIREBASE_CREDENTIALS" ] && [ ! -f "$FIREBASE_CREDENTIALS" ]; then
    echo "Warning: Firebase credentials file not found at $FIREBASE_CREDENTIALS. Continuing without Firebase credentials..."
fi



# Preloading is useful on larger instances but can exceed the 512 MiB Render
# free-tier limit. Keep it configurable; the model is loaded lazily when a
# knowledge operation actually needs it.
if [ "${PRELOAD_MODELS:-true}" = "true" ]; then
    echo "Preloading embedding models..."
    python scripts/preload_models.py
    if [ $? -eq 0 ]; then
        echo "Models preloaded successfully!"
    else
        echo "Warning: Model preloading failed. Continuing anyway..."
    fi
else
    echo "Skipping embedding model preload (PRELOAD_MODELS=${PRELOAD_MODELS})"
fi

# Run migrations
echo "Running database migrations..."
alembic upgrade head

# Use only 2 workers to reduce resource usage and database connection issues
WORKERS=1

# Start the application with Gunicorn
echo "Starting FastAPI application with Gunicorn ($WORKERS workers)..."
gunicorn app.main:app \
    --workers $WORKERS \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:${PORT:-8000} \
    --timeout 120 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    --preload

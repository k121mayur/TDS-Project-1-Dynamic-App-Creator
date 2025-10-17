#!/usr/bin/env bash
set -euo pipefail

export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"

echo "Installing dependencies..."
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "Starting Celery worker..."
celery -A tasks.celery_app worker --loglevel=info &
CELERY_PID=$!

cleanup() {
  echo "Stopping Celery worker..."
  kill "$CELERY_PID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "Starting API server..."
uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"

wait "$CELERY_PID"

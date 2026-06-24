#!/usr/bin/env bash
# tests/run_e2e.sh — start services, run playwright tests, stop services.
# Usage: bash tests/run_e2e.sh [pytest-extra-args]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_PORT=8000
FRONTEND_PORT=3000
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

echo "▶ starting backend (uvicorn main:app --port $BACKEND_PORT)"
uvicorn main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
  --app-dir "$REPO_ROOT" --log-level warning &
PIDS+=($!)

echo "▶ starting frontend (http.server $FRONTEND_PORT)"
python3 -m http.server "$FRONTEND_PORT" --directory "$REPO_ROOT" \
  --bind 127.0.0.1 >/dev/null 2>&1 &
PIDS+=($!)

echo "▶ waiting for services..."
for _ in $(seq 1 20); do
  if curl -sf "http://localhost:$BACKEND_PORT/multiply?a=1&b=1" >/dev/null 2>&1 \
     && curl -sf "http://localhost:$FRONTEND_PORT/" >/dev/null 2>&1; then
    echo "  services ready"
    break
  fi
  sleep 0.5
done

echo "▶ running pytest"
python3 -m pytest "$REPO_ROOT/tests/test_e2e.py" -v "$@"

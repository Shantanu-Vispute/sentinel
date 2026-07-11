#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/venv/bin/python3"
RUNNER="$ROOT/scripts/run-with-status.sh"

if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

mkdir -p "$ROOT/state"
cd "$ROOT"
exec "$RUNNER" raindrop "$ROOT/state/raindrop.log" "Raindrop sync" "$PYTHON" -u -m ingest.raindrop_sync

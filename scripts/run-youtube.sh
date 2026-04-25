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
exec "$RUNNER" youtube "$ROOT/state/youtube.log" "YouTube sync" "$PYTHON" -u -m ingest.youtube_sync

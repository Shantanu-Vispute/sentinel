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
exec "$RUNNER" social "$ROOT/state/social.log" "Social sync" "$PYTHON" -u -m ingest.social_scraper --all

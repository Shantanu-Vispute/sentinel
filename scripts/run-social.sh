#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/venv/bin/python3"

if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

mkdir -p "$ROOT/state"
cd "$ROOT"
exec "$PYTHON" -u -m ingest.social_scraper --all >> "$ROOT/state/social.log" 2>&1

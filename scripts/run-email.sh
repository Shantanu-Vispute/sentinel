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
exec "$RUNNER" email "$ROOT/state/daemon.log" "Email sync" "$PYTHON" -u -m digest.daemon

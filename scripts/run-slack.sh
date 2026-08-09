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
exec "$RUNNER" slack "$ROOT/state/slack.log" "Slack delivery" "$PYTHON" -u -m digest.slack_delivery --run

#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: run-with-status.sh <job> <log-path> <label> <command...>" >&2
  exit 2
fi

JOB="$1"
LOG_PATH="$2"
LABEL="$3"
shift 3

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT/state"
STATUS_PATH="$STATE_DIR/sync_status_${JOB}.json"
PYTHON_BIN="$ROOT/venv/bin/python3"
RUNNER_LOCK_DIR="$STATE_DIR/sync_runner_${JOB}.lockdir"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

mkdir -p "$STATE_DIR"
touch "$LOG_PATH"

if ! mkdir "$RUNNER_LOCK_DIR" 2>/dev/null; then
  exit 75
fi
cleanup_lock() {
  rmdir "$RUNNER_LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM

write_status() {
  local status="$1"
  local ok="$2"
  local message="$3"
  "$PYTHON_BIN" - "$STATUS_PATH" "$JOB" "$LABEL" "$status" "$ok" "$message" "$LOG_PATH" <<'PY'
import json
import os
import sys
from datetime import datetime
from pathlib import Path

status_path = Path(sys.argv[1])
job = sys.argv[2]
label = sys.argv[3]
status = sys.argv[4]
ok = sys.argv[5]
message = sys.argv[6]
log_path = sys.argv[7]

existing = {}
if status_path.exists():
    try:
        existing = json.loads(status_path.read_text())
    except Exception:
        existing = {}

now = datetime.now().isoformat(timespec="seconds")
payload = {
    "job": job,
    "label": label,
    "status": status,
    "ok": ok == "1",
    "pid": os.getpid(),
    "updated_at": now,
    "log_path": log_path,
}
if status == "running":
    payload["started_at"] = now
    payload["finished_at"] = existing.get("finished_at", "")
else:
    payload["started_at"] = existing.get("started_at", now)
    payload["finished_at"] = now
payload["message"] = message
status_path.write_text(json.dumps(payload, indent=2) + "\n")
PY
}

write_status "running" "0" "Starting ${LABEL}"

set +e
"$@" >> "$LOG_PATH" 2>&1
RC=$?
set -e

LAST_LINE="$(tail -n 1 "$LOG_PATH" 2>/dev/null || true)"
if [ "$RC" -eq 0 ]; then
  write_status "success" "1" "${LAST_LINE:-Completed ${LABEL}}"
elif [ "$RC" -eq 75 ] && [[ "$LAST_LINE" == Another\ *\ instance\ is\ already\ running.* ]]; then
  write_status "running" "0" "$LAST_LINE"
else
  write_status "failed" "0" "${LAST_LINE:-${LABEL} failed with exit ${RC}}"
fi

exit "$RC"

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
LOCK_PID_FILE="$RUNNER_LOCK_DIR/pid"

# A job that hangs holds the lock for as long as it lives, so every later run
# exits 75 and the source goes silently stale. Bound the run, and treat a lock
# whose holder is gone (or that outlived its window) as reclaimable.
TIMEOUT_SECS="${SENTINEL_JOB_TIMEOUT:-1800}"
STALE_AFTER=$(( TIMEOUT_SECS + 300 ))

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

mkdir -p "$STATE_DIR"
touch "$LOG_PATH"

# TERM a process and everything below it — killing only the direct child leaves
# grandchildren (playwright's node driver) alive and holding the pipe open.
kill_tree() {
  local pid="$1" sig="${2:-TERM}" kid
  for kid in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$kid" "$sig"
  done
  kill "-$sig" "$pid" 2>/dev/null || true
}

lock_is_live() {
  local holder age
  holder="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  # No pid recorded: a pre-upgrade lock, or the holder died between mkdir and
  # the write. Fall back to age alone.
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    return 1
  fi
  age=$(( $(date +%s) - $(stat -f %m "$RUNNER_LOCK_DIR" 2>/dev/null || echo 0) ))
  [ "$age" -lt "$STALE_AFTER" ]
}

take_lock() {
  mkdir "$RUNNER_LOCK_DIR" 2>/dev/null
}

if ! take_lock; then
  if lock_is_live; then
    exit 75
  fi
  holder="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  echo "[$(date +%FT%T)] runner: reclaiming stale ${JOB} lock (holder=${holder:-unknown})" >> "$LOG_PATH"
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    kill_tree "$holder" TERM
    sleep 5
    kill_tree "$holder" KILL
  fi
  rm -rf "$RUNNER_LOCK_DIR"
  if ! take_lock; then
    exit 75
  fi
fi
echo "$$" > "$LOCK_PID_FILE"
cleanup_lock() {
  rm -rf "$RUNNER_LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT INT TERM

write_status() {
  local status="$1"
  local ok="$2"
  local message="$3"
  "$PYTHON_BIN" - "$STATUS_PATH" "$JOB" "$LABEL" "$status" "$ok" "$message" "$LOG_PATH" "$$" <<'PY'
import json
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
runner_pid = int(sys.argv[8])

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
    "pid": runner_pid,
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
"$@" >> "$LOG_PATH" 2>&1 &
CMD_PID=$!

TIMED_OUT=0
ELAPSED=0
while kill -0 "$CMD_PID" 2>/dev/null; do
  if [ "$ELAPSED" -ge "$TIMEOUT_SECS" ]; then
    TIMED_OUT=1
    echo "[$(date +%FT%T)] runner: ${LABEL} exceeded ${TIMEOUT_SECS}s — killing" >> "$LOG_PATH"
    kill_tree "$CMD_PID" TERM
    sleep 10
    kill_tree "$CMD_PID" KILL
    break
  fi
  sleep 5
  ELAPSED=$(( ELAPSED + 5 ))
done

wait "$CMD_PID"
RC=$?
set -e

LAST_LINE="$(tail -n 1 "$LOG_PATH" 2>/dev/null || true)"
if [ "$TIMED_OUT" -eq 1 ]; then
  write_status "failed" "0" "${LABEL} timed out after ${TIMEOUT_SECS}s — last: ${LAST_LINE}"
  RC=124
elif [ "$RC" -eq 0 ]; then
  write_status "success" "1" "${LAST_LINE:-Completed ${LABEL}}"
elif [ "$RC" -eq 75 ] && [[ "$LAST_LINE" == Another\ *\ instance\ is\ already\ running.* ]]; then
  write_status "running" "0" "$LAST_LINE"
else
  write_status "failed" "0" "${LAST_LINE:-${LABEL} failed with exit ${RC}}"
fi

exit "$RC"

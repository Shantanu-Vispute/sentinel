#!/usr/bin/env bash
set -euo pipefail

BEGIN_MARKER="sentinel cron begin"
END_MARKER="sentinel cron end"
TMP_FILE="$(mktemp)"

if ! crontab -l > "$TMP_FILE" 2>/dev/null; then
  rm -f "$TMP_FILE"
  echo "No crontab found."
  exit 0
fi

awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
  $0 == "# " begin {skip=1; next}
  $0 == "# " end {skip=0; next}
  skip != 1 {print}
' "$TMP_FILE" | crontab -

rm -f "$TMP_FILE"
echo "Removed Sentinel cron jobs."

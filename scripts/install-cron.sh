#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"

if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi

EMAIL_SCHEDULE="${CRON_EMAIL_SCHEDULE:-*/15 * * * *}"
TELEGRAM_SCHEDULE="${CRON_TELEGRAM_SCHEDULE:-*/15 * * * *}"
SOCIAL_SCHEDULE="${CRON_SOCIAL_SCHEDULE:-*/30 * * * *}"
YOUTUBE_SCHEDULE="${CRON_YOUTUBE_SCHEDULE:-5 * * * *}"
BEGIN_MARKER="sentinel cron begin"
END_MARKER="sentinel cron end"
TMP_FILE="$(mktemp)"

chmod +x "$ROOT/scripts/run-email.sh" "$ROOT/scripts/run-telegram.sh" "$ROOT/scripts/run-social.sh" "$ROOT/scripts/run-youtube.sh"

if crontab -l > "$TMP_FILE" 2>/dev/null; then
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    index($0, "\\n# " begin "\\n") {next}
    $0 == "# " begin {skip=1; next}
    $0 == "# " end {skip=0; next}
    skip != 1 {print}
  ' "$TMP_FILE" > "$TMP_FILE.clean"
  mv "$TMP_FILE.clean" "$TMP_FILE"
else
  : > "$TMP_FILE"
fi

{
  cat "$TMP_FILE"
  printf '\n# %s\n' "$BEGIN_MARKER"
  printf 'SHELL=/bin/bash\n'
  printf 'PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin\n'
  printf '%s %q\n' "$EMAIL_SCHEDULE" "$ROOT/scripts/run-email.sh"
  printf '%s %q\n' "$TELEGRAM_SCHEDULE" "$ROOT/scripts/run-telegram.sh"
  printf '%s %q\n' "$SOCIAL_SCHEDULE" "$ROOT/scripts/run-social.sh"
  printf '%s %q\n' "$YOUTUBE_SCHEDULE" "$ROOT/scripts/run-youtube.sh"
  printf '# %s\n' "$END_MARKER"
} | crontab -

rm -f "$TMP_FILE"
echo "Installed Sentinel cron jobs."

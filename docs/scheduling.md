# Sentinel Scheduling

Cron is the default scheduler for periodic sync jobs.

Install jobs:

```bash
scripts/install-cron.sh
```

Remove jobs:

```bash
scripts/uninstall-cron.sh
```

The installer creates one managed block in the current user's crontab. Existing non-managed cron entries are preserved.

Schedules are configured in `.env`:

```bash
CRON_EMAIL_SCHEDULE="*/15 * * * *"
CRON_TELEGRAM_SCHEDULE="*/15 * * * *"
CRON_SOCIAL_SCHEDULE="*/30 * * * *"
CRON_YOUTUBE_SCHEDULE="5 * * * *"
```

Runner scripts write logs to `state/`:

```text
state/daemon.log
state/telegram.log
state/social.log
state/youtube.log
```

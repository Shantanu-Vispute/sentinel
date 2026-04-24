# Sentinel

Local intelligence for the things you read, save, and follow.

Sentinel turns newsletters, saved posts, channels, and Watch Later into a private local digest.

## Setup

Use Python 3.11 or 3.12. Then install Python dependencies:

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Install and start Ollama, then pull the configured models:

```bash
ollama serve
ollama pull gemma4:e2b
ollama pull qwen3-embedding:0.6b
```

Create local config:

```bash
cp .env.example .env
```

Edit `.env` for your machine. The common fields are:

```bash
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma4:e2b
EMBEDDING_MODEL=qwen3-embedding:0.6b
TELEGRAM_CHANNELS=channel_one,channel_two
GMAIL_CREDENTIALS_PATH=state/credentials.json
GMAIL_TOKEN_PATH=state/token.json
STORIES_DB=state/stories.db
```

Everything under `state/` is local runtime data and is intentionally ignored by git.

## Platform Login

### Gmail

1. Create a Google Cloud project.
2. Enable the Gmail API.
3. Create an OAuth client of type `Desktop app`.
4. Download the OAuth client JSON.
5. Save it at `state/credentials.json`, or set `GMAIL_CREDENTIALS_PATH` in `.env`.

First run opens a browser for Google consent and writes the token to `state/token.json`:

```bash
python -m digest.daemon --max-results 20
```

Later runs reuse the saved token.

### Telegram

Telegram uses public web pages, so no login is needed. Add public channel handles to `.env` without `@`:

```bash
TELEGRAM_CHANNELS=channel_one,channel_two
```

Run:

```bash
python -m digest.daemon --telegram
```

Backfill from a date:

```bash
python -m digest.daemon --telegram --telegram-since 2026-04-01
```

### X/Twitter and LinkedIn

The scraper stores a local browser session in `state/browser_profile`.

Open the login browser:

```bash
python -m ingest.social_scraper --login
```

Log in to X/Twitter and LinkedIn in the opened tabs, then close the browser window.

Run a quick scrape:

```bash
python -m ingest.social_scraper --source twitter --limit 20
python -m ingest.social_scraper --source linkedin --limit 20
```

Run both:

```bash
python -m ingest.social_scraper --all
```

### YouTube Watch Later

YouTube also uses `state/browser_profile`. Run the same login helper and log in to YouTube in the opened tab:

```bash
python -m ingest.social_scraper --login
```

Then export cookies for `yt-dlp`:

```bash
python -m ingest.youtube_sync --export-cookies
```

Sync Watch Later:

```bash
python -m ingest.youtube_sync
```

## Run The App

Start the web UI:

```bash
source venv/bin/activate
flask --app app run --host 127.0.0.1 --port 5000
```

Open:

```text
http://127.0.0.1:5000/digest
```

Useful filters:

```text
http://127.0.0.1:5000/digest?source=email
http://127.0.0.1:5000/digest?source=telegram
http://127.0.0.1:5000/digest?source=twitter
http://127.0.0.1:5000/digest?source=linkedin
http://127.0.0.1:5000/digest?source=youtube
```

## Scheduled Runs

Configure schedules in `.env`:

```bash
CRON_EMAIL_SCHEDULE="*/15 * * * *"
CRON_TELEGRAM_SCHEDULE="*/15 * * * *"
CRON_SOCIAL_SCHEDULE="*/30 * * * *"
CRON_YOUTUBE_SCHEDULE="5 * * * *"
```

Install cron jobs:

```bash
scripts/install-cron.sh
```

Remove cron jobs:

```bash
scripts/uninstall-cron.sh
```

Logs are written to:

```text
state/daemon.log
state/telegram.log
state/social.log
state/youtube.log
```

## Common Commands

```bash
python -m digest.daemon --max-results 50
python -m digest.daemon --since 2026-04-01
python -m digest.daemon --telegram
python -m ingest.social_scraper --all
python -m ingest.youtube_sync
flask --app app run --host 127.0.0.1 --port 5000
```

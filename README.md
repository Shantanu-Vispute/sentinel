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

Create local config:

```bash
cp .env.example .env
```

Gemini is the default backend. Add your Gemini API key to `.env`:

```bash
LLM_PROVIDER=gemini
EMBEDDING_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key
GEMINI_API_KEYS=your_gemini_api_key,your_second_gemini_api_key
GEMINI_EMBEDDING_MODEL=gemini-embedding-001
```

`GEMINI_API_KEY` is still supported for one key. `GEMINI_API_KEYS` can hold a comma-separated list; Sentinel will rotate to the next key on Gemini rate-limit or quota errors. Sentinel will also try Gemini chat models in this order on rate-limit or quota errors:

```text
gemini-3.1-flash-lite-preview
gemini-2.5-flash-lite
gemini-2.5-flash
gemini-3-flash-preview
```

If you want a fully local setup, switch both providers to Ollama and start the Ollama models:

```bash
LLM_PROVIDER=ollama
EMBEDDING_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma4:e2b
EMBEDDING_MODEL=qwen3-embedding:0.6b
ollama serve
ollama pull gemma4:e2b
ollama pull qwen3-embedding:0.6b
```

Other common `.env` fields:

```bash
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

The digest toolbar also includes a manual sync button. It can trigger `all`, or the current filtered source, and the dashboard shows live sync status for `email`, `telegram`, `social`, and `youtube`.

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

Live sync state is also written to:

```text
state/sync_status_email.json
state/sync_status_telegram.json
state/sync_status_social.json
state/sync_status_youtube.json
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

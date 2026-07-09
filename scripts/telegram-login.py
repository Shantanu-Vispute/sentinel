#!/usr/bin/env python3
"""One-time interactive Telegram login for the Telethon-based fetcher.

Run this manually (never from cron): it prompts for your phone number,
the login code Telegram sends you, and your 2FA password if you have one
enabled. On success it saves a session file at TELEGRAM_SESSION_PATH that
later daemon runs reuse without prompting again, then lists the channels
and groups you're in so you can copy handles into TELEGRAM_CHANNELS.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telethon.sync import TelegramClient
from telethon.tl.types import Channel

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH


def main():
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set.\n"
            "Get them from https://my.telegram.org (API Development tools) "
            "and add them to .env, then re-run this script."
        )
        sys.exit(1)

    print(f"Session will be saved at: {TELEGRAM_SESSION_PATH}.session")
    with TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH) as client:
        me = client.get_me()
        print(f"\nLogged in as: {me.first_name or ''} (@{me.username or me.id})\n")

        print("Channels and groups you're in:\n")
        for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, Channel):
                continue
            handle = f"@{entity.username}" if entity.username else f"id:{entity.id} (private, no username)"
            kind = "channel" if entity.broadcast else "group"
            print(f"  {handle:<40} {kind}")

        print(
            "\nAdd the handles you want ingested (without @) as a comma-separated "
            "TELEGRAM_CHANNELS list in .env. Private channels/groups without a "
            "public username aren't supported by TELEGRAM_CHANNELS yet."
        )


if __name__ == "__main__":
    main()

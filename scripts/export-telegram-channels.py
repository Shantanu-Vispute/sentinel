#!/usr/bin/env python3
"""One-off export: list channels/groups you're in and their recent messages.

Writes two CSVs into state/:
  telegram_channels.csv       - one row per channel/group
  telegram_recent_messages.csv - last N messages per channel/group

Read-only. Does not touch the stories DB or ingestion pipeline.
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from telethon.sync import TelegramClient
from telethon.tl.types import Channel, Chat

from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH

STATE_DIR = ROOT / "state"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages-per-channel", type=int, default=6)
    args = parser.parse_args()

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH not set in .env.")
        sys.exit(1)

    client = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    client.connect()
    if not client.is_user_authorized():
        print("Not authorized. Run scripts/telegram-login.py first.")
        sys.exit(1)

    channels_path = STATE_DIR / "telegram_channels.csv"
    messages_path = STATE_DIR / "telegram_recent_messages.csv"

    channel_rows = []
    message_rows = []

    for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, (Channel, Chat)):
            continue
        if isinstance(entity, Channel):
            kind = "channel" if entity.broadcast else "group"
            handle = entity.username or ""
        else:
            kind = "group"
            handle = ""

        channel_rows.append({
            "id": entity.id,
            "title": dialog.name or "",
            "handle": f"@{handle}" if handle else "",
            "kind": kind,
            "members": getattr(entity, "participants_count", "") or "",
        })

        try:
            for message in client.iter_messages(entity, limit=args.messages_per_channel):
                if message is None or (not message.raw_text and not message.media):
                    continue
                message_rows.append({
                    "channel_title": dialog.name or "",
                    "channel_handle": f"@{handle}" if handle else "",
                    "msg_id": message.id,
                    "date": message.date.isoformat(),
                    "text": (message.raw_text or "").replace("\n", " ")[:500],
                    "has_media": bool(message.media),
                })
        except Exception as exc:
            print(f"  skipped messages for {dialog.name!r}: {exc}")

    client.disconnect()

    with channels_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "handle", "kind", "members"])
        writer.writeheader()
        writer.writerows(channel_rows)

    with messages_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["channel_title", "channel_handle", "msg_id", "date", "text", "has_media"],
        )
        writer.writeheader()
        writer.writerows(message_rows)

    print(f"{len(channel_rows)} channels/groups -> {channels_path}")
    print(f"{len(message_rows)} messages -> {messages_path}")


if __name__ == "__main__":
    main()

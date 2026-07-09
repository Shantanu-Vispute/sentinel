#!/usr/bin/env python3
import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import STORIES_DB
from digest.media_cache import cache_remote_image, is_local_media_url, remote_host
from ingest.telegram_fetcher import _extract_images, _extract_video_thumbs


TG_URL_RE = re.compile(r"https?://t\.me/([^/\s]+)/(\d+)")
UA = "Mozilla/5.0 (Sentinel telegram image repair)"


def _message_media_url(tg_url: str) -> str:
    match = TG_URL_RE.match(tg_url or "")
    if not match:
        return ""
    channel, msg_id_s = match.groups()
    msg_id = int(msg_id_s)

    candidates = [
        f"https://t.me/s/{channel}/{msg_id}",
        f"https://t.me/s/{channel}?before={msg_id + 1}",
    ]
    wanted = f"https://t.me/{channel}/{msg_id}"

    for url in candidates:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for wrap in soup.select(".tgme_widget_message_wrap"):
            date_el = wrap.select_one(".tgme_widget_message_date")
            if not date_el or date_el.get("href", "") != wanted:
                continue
            images = _extract_images(wrap)
            if images:
                return images[0]
            thumbs = _extract_video_thumbs(wrap)
            if thumbs:
                return thumbs[0]
    return ""


def repair(limit: int | None = None, sleep_seconds: float = 0.4) -> tuple[int, int, int]:
    db_path = Path(STORIES_DB)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT s.id, s.primary_image_url, m.gmail_url
             FROM stories s
             JOIN mentions m ON m.story_id = s.id
            WHERE COALESCE(s.source_type, 'email') = 'telegram'
              AND COALESCE(s.primary_image_url, '') != ''
            ORDER BY s.last_updated ASC"""
    ).fetchall()
    if limit:
        rows = rows[:limit]

    checked = updated = failed = 0
    for row in rows:
        current = row["primary_image_url"] or ""
        if is_local_media_url(current):
            continue
        checked += 1
        try:
            if "telesco.pe" in remote_host(current):
                fresh = _message_media_url(row["gmail_url"] or "")
                if not fresh:
                    raise RuntimeError("no fresh media URL found")
                local_url = cache_remote_image(fresh, "telegram")
            else:
                local_url = cache_remote_image(current, "telegram")

            conn.execute(
                "UPDATE stories SET primary_image_url = ? WHERE id = ?",
                (local_url, row["id"]),
            )
            conn.commit()
            updated += 1
            print(f"updated {row['id'][:8]} -> {local_url}", flush=True)
        except Exception as exc:
            failed += 1
            print(f"failed  {row['id'][:8]} {row['gmail_url']}: {exc}", flush=True)
        time.sleep(sleep_seconds)

    conn.close()
    return checked, updated, failed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=0.4)
    args = parser.parse_args()
    checked, updated, failed = repair(limit=args.limit, sleep_seconds=args.sleep)
    print(f"checked={checked} updated={updated} failed={failed}", flush=True)


if __name__ == "__main__":
    main()

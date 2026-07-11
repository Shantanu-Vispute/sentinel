"""Synchronize the Raindrop.io library into Sentinel's local bookmark DB."""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

from config import BOOKMARKS_DB, RAINDROP_API_BASE, RAINDROP_TOKEN


DB_PATH = Path(BOOKMARKS_DB)


def _parse_ts(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _require_token() -> str:
    if RAINDROP_TOKEN:
        return RAINDROP_TOKEN
    raise RuntimeError(
        "RAINDROP_TOKEN is not configured in Sentinel's .env file"
    )


def ensure_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS raindrop_bookmarks (
            id TEXT PRIMARY KEY,
            title TEXT,
            note TEXT,
            excerpt TEXT,
            url TEXT,
            domain TEXT,
            folder TEXT,
            tags TEXT,
            created TEXT,
            created_ts INTEGER,
            last_update_ts INTEGER,
            cover TEXT,
            favorite INTEGER DEFAULT 0,
            synced_at INTEGER NOT NULL
        )"""
    )
    conn.commit()
    conn.close()


def fetch_collections(session: requests.Session) -> dict[str, str]:
    collections: dict[str, str] = {}
    for path in ("/collections", "/collections/childrens"):
        response = session.get(f"{RAINDROP_API_BASE}{path}", timeout=20)
        response.raise_for_status()
        for collection in response.json().get("items", []):
            collections[str(collection["_id"])] = collection.get("title") or ""
    collections.update({"0": "All", "-1": "Unsorted", "-99": "Trash"})
    return collections


def fetch_page(session: requests.Session, page: int) -> dict:
    response = session.get(
        f"{RAINDROP_API_BASE}/raindrops/0",
        params={"page": page, "perpage": 50, "sort": "-lastUpdate"},
        timeout=30,
    )
    if response.status_code == 429:
        time.sleep(int(response.headers.get("Retry-After", "5") or 5))
        return fetch_page(session, page)
    response.raise_for_status()
    return response.json()


def _existing_updates() -> dict[str, int]:
    if not DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, COALESCE(last_update_ts, 0) FROM raindrop_bookmarks"
        ).fetchall()
        return {row[0]: int(row[1] or 0) for row in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _upsert(items: list[dict], collections: dict[str, str]) -> int:
    if not items:
        return 0
    now = int(time.time())
    rows = []
    for item in items:
        collection_id = str((item.get("collection") or {}).get("$id", ""))
        tags = item.get("tags") or []
        rows.append(
            (
                str(item["_id"]),
                (item.get("title") or "").strip(),
                (item.get("note") or "").strip(),
                (item.get("excerpt") or "").strip(),
                (item.get("link") or "").strip(),
                (item.get("domain") or "").strip(),
                collections.get(collection_id, collection_id),
                ", ".join(tags) if isinstance(tags, list) else "",
                item.get("created") or "",
                _parse_ts(item.get("created")),
                _parse_ts(item.get("lastUpdate")),
                (item.get("cover") or "").strip(),
                1 if item.get("important") else 0,
                now,
            )
        )
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        """INSERT INTO raindrop_bookmarks (
               id, title, note, excerpt, url, domain, folder, tags,
               created, created_ts, last_update_ts, cover, favorite, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, note=excluded.note, excerpt=excluded.excerpt,
             url=excluded.url, domain=excluded.domain, folder=excluded.folder,
             tags=excluded.tags, created=excluded.created,
             created_ts=excluded.created_ts, last_update_ts=excluded.last_update_ts,
             cover=excluded.cover, favorite=excluded.favorite,
             synced_at=excluded.synced_at""",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def run(full: bool = False) -> tuple[int, int]:
    ensure_schema()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {_require_token()}"})
    known_updates = _existing_updates()
    collections = fetch_collections(session)
    fetched = upserted = 0
    page = 0
    while True:
        items = fetch_page(session, page).get("items", []) or []
        if not items:
            break
        fetched += len(items)
        if full:
            candidates = items
        else:
            candidates = [
                item for item in items
                if str(item["_id"]) not in known_updates
                or _parse_ts(item.get("lastUpdate")) > known_updates.get(str(item["_id"]), 0)
            ]
        upserted += _upsert(candidates, collections)
        page += 1
        time.sleep(0.5)
    return fetched, upserted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="rewrite the full Raindrop library")
    args = parser.parse_args()
    fetched, upserted = run(full=args.full)
    print(f"Raindrop sync complete: fetched={fetched} upserted={upserted}")


if __name__ == "__main__":
    main()

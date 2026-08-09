"""Durable Slack reconciliation, backfill, and delivery worker for Sentinel."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from config import (
    BOOKMARKS_DB,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_CANONICAL,
    SLACK_CHANNELS,
    SLACK_ENABLED,
    SLACK_WORKER_MAX_RUNTIME_SECONDS,
    STORIES_DB,
)
from digest.slack_client import SlackAPIError, SlackClient


DB_PATH = Path(STORIES_DB)
BOOKMARKS_DB_PATH = Path(BOOKMARKS_DB)
APP_TIMEZONE = ZoneInfo("Asia/Kolkata")
PER_CHANNEL_INTERVAL_SECONDS = 1.05


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS slack_outbox (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key        TEXT NOT NULL UNIQUE,
            event_type       TEXT NOT NULL,
            entity_type      TEXT NOT NULL,
            entity_id        TEXT NOT NULL,
            story_id         TEXT,
            source_type      TEXT NOT NULL,
            channel_id       TEXT NOT NULL,
            payload_json     TEXT NOT NULL,
            parent_event_key TEXT,
            event_ts         INTEGER NOT NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            attempt_count    INTEGER NOT NULL DEFAULT 0,
            available_at     TEXT,
            last_error       TEXT,
            slack_ts         TEXT,
            locked_at        TEXT,
            created_at       TEXT NOT NULL,
            sent_at          TEXT
        );
        CREATE TABLE IF NOT EXISTS slack_roots (
            entity_type TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            channel_id  TEXT NOT NULL,
            root_ts     TEXT NOT NULL,
            permalink   TEXT,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (entity_type, entity_id)
        );
        CREATE TABLE IF NOT EXISTS slack_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_slack_outbox_ready
            ON slack_outbox(status, available_at, event_ts, id);
        CREATE INDEX IF NOT EXISTS idx_slack_outbox_entity
            ON slack_outbox(entity_type, entity_id);
        """
    )
    timeline_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(timeline_entries)")
    }
    if timeline_columns and "mention_id" not in timeline_columns:
        conn.execute(
            "ALTER TABLE timeline_entries ADD COLUMN mention_id INTEGER"
        )
    if timeline_columns:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_timeline_mention ON timeline_entries(mention_id)"
        )
    conn.commit()


def configuration_errors(*, require_token: bool = True) -> list[str]:
    errors: list[str] = []
    if require_token and not SLACK_BOT_TOKEN:
        errors.append("SLACK_BOT_TOKEN is missing")
    if not SLACK_CHANNEL_CANONICAL:
        errors.append("SLACK_CHANNEL_CANONICAL is missing")
    for source, channel_id in SLACK_CHANNELS.items():
        if not channel_id:
            errors.append(f"Slack channel for {source} is missing")
    configured = [SLACK_CHANNEL_CANONICAL, *SLACK_CHANNELS.values()]
    configured = [value for value in configured if value]
    if len(configured) != len(set(configured)):
        errors.append("Slack channel IDs must be unique")
    return errors


def _state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM slack_state WHERE key=?", (key,)
    ).fetchone()
    return str(row[0]) if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: Any) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO slack_state(key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             value=excluded.value, updated_at=excluded.updated_at""",
        (key, str(value), now),
    )


def _refresh_unsent_channels(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE slack_outbox SET channel_id=?
           WHERE status!='sent'
             AND event_type IN ('canonical_root', 'thread_update')""",
        (SLACK_CHANNEL_CANONICAL,),
    )
    for source, channel_id in SLACK_CHANNELS.items():
        if channel_id:
            conn.execute(
                """UPDATE slack_outbox SET channel_id=?
                   WHERE status!='sent' AND event_type='source_post'
                     AND source_type=?""",
                (channel_id, source),
            )


def retry_blocked() -> int:
    errors = configuration_errors(require_token=False)
    if errors:
        raise RuntimeError("; ".join(errors))
    conn = _connect()
    _refresh_unsent_channels(conn)
    cursor = conn.execute(
        """UPDATE slack_outbox SET status='pending', attempt_count=0,
                  available_at=NULL, last_error=NULL, locked_at=NULL
           WHERE status='blocked'"""
    )
    conn.commit()
    count = int(cursor.rowcount)
    conn.close()
    return count


def _epoch(value: Any, fallback: int = 0) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        return int(float(raw))
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        try:
            return int(parsedate_to_datetime(raw).timestamp())
        except (TypeError, ValueError, OverflowError):
            return fallback


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _escape(value: Any) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _linked_title(title: str, url: str) -> str:
    safe_title = _escape(_clean(title, 200) or "Untitled")
    return f"<{url}|{safe_title}>" if url else f"*{safe_title}*"


def _date_label(value: Any) -> str:
    timestamp = _epoch(value)
    if not timestamp:
        return "date unavailable"
    dt = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(APP_TIMEZONE)
    return dt.strftime("%Y-%m-%d %H:%M IST")


def build_message(
    event_type: str, payload: dict[str, Any], permalink: str = ""
) -> tuple[str, list[dict[str, Any]]]:
    title = _clean(payload.get("title"), 200) or "Untitled"
    summary = _clean(payload.get("summary"), 700)
    url = str(payload.get("url") or "")
    source = _clean(payload.get("source"), 50) or "unknown"
    sender = _clean(payload.get("sender"), 100)
    category = _clean(payload.get("category"), 80)
    changed = _clean(payload.get("what_changed"), 900)
    date_label = _date_label(payload.get("date"))

    if event_type == "thread_update":
        heading = f"*Also covered by {_escape(source)}*"
        if payload.get("new_source") and changed:
            heading = f"*New source: {_escape(source)} · Story evolved*"
        elif changed:
            heading = "*Story evolved*"
        body_parts = [heading]
        if changed:
            body_parts.append(f">{_escape(changed)}")
        if url:
            body_parts.append(f"<{url}|View new coverage>")
        section_text = "\n".join(body_parts)
    else:
        section_parts = [_linked_title(title, url)]
        if summary:
            section_parts.append(f">{_escape(summary)}")
        section_text = "\n".join(section_parts)

    context = [f"Source: {_escape(source)}", date_label]
    if sender and sender.lower() != source.lower():
        context.insert(1, f"By: {_escape(sender)}")
    if category:
        context.append(f"Category: {_escape(category)}")

    fallback_parts = [title]
    if changed:
        fallback_parts.append(changed)
    elif summary:
        fallback_parts.append(summary)
    if url:
        fallback_parts.append(url)
    text = _escape(" — ".join(fallback_parts))
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": section_text},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": " · ".join(context)}
            ],
        },
    ]
    if permalink:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View merged timeline"},
                        "url": permalink,
                        "action_id": "view_merged_timeline",
                    }
                ],
            }
        )
    return _clean(text, 3000), blocks


def _enqueue(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    source_type: str,
    channel_id: str,
    payload: dict[str, Any],
    event_ts: int,
    story_id: str | None = None,
    parent_event_key: str | None = None,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO slack_outbox
           (event_key, event_type, entity_type, entity_id, story_id,
            source_type, channel_id, payload_json, parent_event_key,
            event_ts, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            event_key,
            event_type,
            entity_type,
            entity_id,
            story_id,
            source_type,
            channel_id,
            json.dumps(payload, ensure_ascii=False),
            parent_event_key,
            max(0, int(event_ts)),
            now,
        ),
    )
    return cursor.rowcount > 0


def _story_payload(story: sqlite3.Row, mention: sqlite3.Row) -> dict[str, Any]:
    return {
        "title": story["title"] or mention["title"] or "Untitled",
        "summary": story["summary"] or mention["summary"] or "",
        "url": story["primary_url"] or mention["gmail_url"] or "",
        "source": mention["source_type"] or story["source_type"] or "email",
        "sender": mention["sender"] or "",
        "category": story["category"] or "",
        "date": mention["date"] or story["first_seen"],
    }


def _mention_payload(story: sqlite3.Row, mention: sqlite3.Row) -> dict[str, Any]:
    return {
        "title": mention["title"] or story["title"] or "Untitled",
        "summary": mention["summary"] or story["summary"] or "",
        "url": mention["gmail_url"] or story["primary_url"] or "",
        "source": mention["source_type"] or story["source_type"] or "email",
        "sender": mention["sender"] or "",
        "category": story["category"] or "",
        "date": mention["date"] or mention["created_at"],
    }


def _enqueue_story_root(
    conn: sqlite3.Connection, story: sqlite3.Row, mention: sqlite3.Row
) -> int:
    story_id = str(story["id"])
    source = str(mention["source_type"] or story["source_type"] or "email")
    source_channel = SLACK_CHANNELS.get(source, "")
    root_key = f"root:story:{story_id}"
    event_ts = _epoch(mention["date"], _epoch(story["first_seen"]))
    payload = _story_payload(story, mention)
    inserted = int(
        _enqueue(
            conn,
            event_key=root_key,
            event_type="canonical_root",
            entity_type="story",
            entity_id=story_id,
            story_id=story_id,
            source_type=source,
            channel_id=SLACK_CHANNEL_CANONICAL,
            payload=payload,
            event_ts=event_ts,
        )
    )
    if source_channel:
        inserted += int(
            _enqueue(
                conn,
                event_key=f"source:story:{story_id}:mention:{mention['id']}",
                event_type="source_post",
                entity_type="story",
                entity_id=story_id,
                story_id=story_id,
                source_type=source,
                channel_id=source_channel,
                payload=_mention_payload(story, mention),
                parent_event_key=root_key,
                event_ts=event_ts,
            )
        )
    return inserted


def _enqueue_external_bookmark(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> int:
    entity_id = str(row["id"])
    source = str(row["source"] or "twitter")
    root_key = f"root:external_bookmark:{entity_id}"
    event_ts = int(row["created_ts"] or row["scraped_at"] or 0)
    payload = {
        "title": row["title"] or row["url"] or "Untitled",
        "summary": row["excerpt"] or "",
        "url": row["url"] or "",
        "source": source,
        "sender": row["author"] or "",
        "category": "bookmark",
        "date": event_ts,
    }
    inserted = int(
        _enqueue(
            conn,
            event_key=root_key,
            event_type="canonical_root",
            entity_type="external_bookmark",
            entity_id=entity_id,
            source_type=source,
            channel_id=SLACK_CHANNEL_CANONICAL,
            payload=payload,
            event_ts=event_ts,
        )
    )
    channel_id = SLACK_CHANNELS.get(source, "")
    if channel_id:
        inserted += int(
            _enqueue(
                conn,
                event_key=f"source:external_bookmark:{entity_id}",
                event_type="source_post",
                entity_type="external_bookmark",
                entity_id=entity_id,
                source_type=source,
                channel_id=channel_id,
                payload=payload,
                parent_event_key=root_key,
                event_ts=event_ts,
            )
        )
    return inserted


def _enqueue_raindrop_bookmark(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> int:
    entity_id = str(row["id"])
    root_key = f"root:raindrop_bookmark:{entity_id}"
    event_ts = int(row["created_ts"] or row["synced_at"] or 0)
    payload = {
        "title": row["title"] or row["url"] or "Untitled",
        "summary": row["excerpt"] or row["note"] or "",
        "url": row["url"] or "",
        "source": "raindrop",
        "sender": row["folder"] or "",
        "category": "bookmark",
        "date": event_ts,
    }
    inserted = int(
        _enqueue(
            conn,
            event_key=root_key,
            event_type="canonical_root",
            entity_type="raindrop_bookmark",
            entity_id=entity_id,
            source_type="raindrop",
            channel_id=SLACK_CHANNEL_CANONICAL,
            payload=payload,
            event_ts=event_ts,
        )
    )
    channel_id = SLACK_CHANNELS.get("raindrop", "")
    if channel_id:
        inserted += int(
            _enqueue(
                conn,
                event_key=f"source:raindrop_bookmark:{entity_id}",
                event_type="source_post",
                entity_type="raindrop_bookmark",
                entity_id=entity_id,
                source_type="raindrop",
                channel_id=channel_id,
                payload=payload,
                parent_event_key=root_key,
                event_ts=event_ts,
            )
        )
    return inserted


def preview_backfill() -> dict[str, Any]:
    result: dict[str, Any] = {
        "stories": 0,
        "external_bookmarks": {},
        "raindrop_bookmarks": 0,
    }
    if DB_PATH.exists():
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        result["stories"] = int(
            conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0]
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_bookmarks'"
        ).fetchone():
            result["external_bookmarks"] = {
                row[0]: int(row[1])
                for row in conn.execute(
                    "SELECT source, COUNT(*) FROM external_bookmarks GROUP BY source"
                )
            }
        conn.close()
    if BOOKMARKS_DB_PATH.exists():
        conn = sqlite3.connect(f"file:{BOOKMARKS_DB_PATH}?mode=ro", uri=True)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raindrop_bookmarks'"
        ).fetchone():
            result["raindrop_bookmarks"] = int(
                conn.execute("SELECT COUNT(*) FROM raindrop_bookmarks").fetchone()[0]
            )
        conn.close()
    roots = (
        result["stories"]
        + sum(result["external_bookmarks"].values())
        + result["raindrop_bookmarks"]
    )
    result["canonical_roots"] = roots
    result["source_posts"] = roots
    result["total_messages"] = roots * 2
    return result


def initialize_backfill() -> dict[str, int]:
    errors = configuration_errors(require_token=False)
    if errors:
        raise RuntimeError("; ".join(errors))
    conn = _connect()
    if _state(conn, "backfill_initialized") == "1":
        conn.close()
        raise RuntimeError("Slack backfill is already initialized")
    counts = {"stories": 0, "external_bookmarks": 0, "raindrop_bookmarks": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        _refresh_unsent_channels(conn)
        story_rows = conn.execute(
            "SELECT * FROM stories ORDER BY first_seen ASC, id ASC"
        ).fetchall()
        for story in story_rows:
            mention = conn.execute(
                "SELECT * FROM mentions WHERE story_id=? ORDER BY id ASC LIMIT 1",
                (story["id"],),
            ).fetchone()
            if mention:
                counts["stories"] += _enqueue_story_root(conn, story, mention)

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_bookmarks'"
        ).fetchone():
            rows = conn.execute(
                "SELECT * FROM external_bookmarks ORDER BY COALESCE(created_ts, scraped_at), id"
            ).fetchall()
            for row in rows:
                counts["external_bookmarks"] += _enqueue_external_bookmark(conn, row)

        if BOOKMARKS_DB_PATH.exists():
            bookmark_conn = sqlite3.connect(
                f"file:{BOOKMARKS_DB_PATH}?mode=ro", uri=True
            )
            bookmark_conn.row_factory = sqlite3.Row
            if bookmark_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raindrop_bookmarks'"
            ).fetchone():
                rows = bookmark_conn.execute(
                    "SELECT * FROM raindrop_bookmarks ORDER BY created_ts, id"
                ).fetchall()
                for row in rows:
                    counts["raindrop_bookmarks"] += _enqueue_raindrop_bookmark(conn, row)
            bookmark_conn.close()

        max_mention = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM mentions").fetchone()[0]
        )
        max_timeline = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM timeline_entries").fetchone()[0]
        )
        now = datetime.now(timezone.utc).isoformat()
        _set_state(conn, "backfill_initialized", "1")
        _set_state(conn, "backfill_completed", "0")
        _set_state(conn, "backfill_cutoff", now)
        _set_state(conn, "mention_checkpoint", max_mention)
        _set_state(conn, "timeline_checkpoint", max_timeline)
        _set_state(conn, "historical_mentions_suppressed_through", max_mention)
        _set_state(conn, "historical_timeline_suppressed_through", max_timeline)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def _load_story_and_first_mention(
    conn: sqlite3.Connection, story_id: str
) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    story = conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not story:
        return None
    mention = conn.execute(
        "SELECT * FROM mentions WHERE story_id=? ORDER BY id ASC LIMIT 1",
        (story_id,),
    ).fetchone()
    return (story, mention) if mention else None


def reconcile() -> dict[str, int]:
    conn = _connect()
    if _state(conn, "backfill_initialized") != "1":
        conn.close()
        raise RuntimeError(
            "Slack backfill is not initialized; run --preview-backfill and --init-backfill first"
        )
    counts = {"roots": 0, "source_posts": 0, "thread_updates": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        missing_story_ids = [
            row[0]
            for row in conn.execute(
                """SELECT s.id FROM stories s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM slack_outbox o
                        WHERE o.event_key = 'root:story:' || s.id
                   )
                   ORDER BY s.first_seen, s.id"""
            )
        ]
        for story_id in missing_story_ids:
            pair = _load_story_and_first_mention(conn, story_id)
            if pair:
                inserted = _enqueue_story_root(conn, *pair)
                counts["roots"] += min(inserted, 1)
                counts["source_posts"] += max(0, inserted - 1)

        mention_checkpoint = int(_state(conn, "mention_checkpoint", "0") or 0)
        mentions = conn.execute(
            "SELECT * FROM mentions WHERE id>? ORDER BY id ASC",
            (mention_checkpoint,),
        ).fetchall()
        for mention in mentions:
            story = conn.execute(
                "SELECT * FROM stories WHERE id=?", (mention["story_id"],)
            ).fetchone()
            if not story:
                continue
            source = str(mention["source_type"] or story["source_type"] or "email")
            source_channel = SLACK_CHANNELS.get(source, "")
            root_key = f"root:story:{story['id']}"
            event_ts = _epoch(mention["created_at"], _epoch(mention["date"]))
            if source_channel and _enqueue(
                conn,
                event_key=f"source:story:{story['id']}:mention:{mention['id']}",
                event_type="source_post",
                entity_type="story",
                entity_id=str(story["id"]),
                story_id=str(story["id"]),
                source_type=source,
                channel_id=source_channel,
                payload=_mention_payload(story, mention),
                parent_event_key=root_key,
                event_ts=event_ts,
            ):
                counts["source_posts"] += 1

            first_id = int(
                conn.execute(
                    "SELECT MIN(id) FROM mentions WHERE story_id=?",
                    (story["id"],),
                ).fetchone()[0]
            )
            prior_same_source = conn.execute(
                """SELECT 1 FROM mentions
                   WHERE story_id=? AND id<? AND COALESCE(source_type, 'email')=?
                   LIMIT 1""",
                (story["id"], mention["id"], source),
            ).fetchone()
            new_source = int(mention["id"]) != first_id and prior_same_source is None
            timeline = conn.execute(
                "SELECT * FROM timeline_entries WHERE mention_id=? ORDER BY id ASC LIMIT 1",
                (mention["id"],),
            ).fetchone()
            if new_source or timeline:
                payload = _mention_payload(story, mention)
                payload["new_source"] = bool(new_source)
                payload["what_changed"] = timeline["what_changed"] if timeline else ""
                if _enqueue(
                    conn,
                    event_key=f"thread:story:{story['id']}:mention:{mention['id']}",
                    event_type="thread_update",
                    entity_type="story",
                    entity_id=str(story["id"]),
                    story_id=str(story["id"]),
                    source_type=source,
                    channel_id=SLACK_CHANNEL_CANONICAL,
                    payload=payload,
                    parent_event_key=root_key,
                    event_ts=event_ts,
                ):
                    counts["thread_updates"] += 1

        if mentions:
            _set_state(conn, "mention_checkpoint", mentions[-1]["id"])

        timeline_checkpoint = int(_state(conn, "timeline_checkpoint", "0") or 0)
        timelines = conn.execute(
            """SELECT * FROM timeline_entries
               WHERE id>? ORDER BY id ASC""",
            (timeline_checkpoint,),
        ).fetchall()
        for timeline in timelines:
            story = conn.execute(
                "SELECT * FROM stories WHERE id=?", (timeline["story_id"],)
            ).fetchone()
            if not story:
                continue
            if timeline["mention_id"] is not None:
                mention = conn.execute(
                    "SELECT * FROM mentions WHERE id=?",
                    (timeline["mention_id"],),
                ).fetchone()
                if not mention:
                    continue
                source = str(
                    mention["source_type"] or story["source_type"] or "email"
                )
                first_id = int(
                    conn.execute(
                        "SELECT MIN(id) FROM mentions WHERE story_id=?",
                        (story["id"],),
                    ).fetchone()[0]
                )
                prior_same_source = conn.execute(
                    """SELECT 1 FROM mentions
                       WHERE story_id=? AND id<?
                         AND COALESCE(source_type, 'email')=? LIMIT 1""",
                    (story["id"], mention["id"], source),
                ).fetchone()
                payload = _mention_payload(story, mention)
                payload["new_source"] = (
                    int(mention["id"]) != first_id and prior_same_source is None
                )
                payload["what_changed"] = timeline["what_changed"]
                combined_key = (
                    f"thread:story:{story['id']}:mention:{mention['id']}"
                )
                existing_event = conn.execute(
                    "SELECT id, status, payload_json FROM slack_outbox WHERE event_key=?",
                    (combined_key,),
                ).fetchone()
                if existing_event and existing_event["status"] in {
                    "pending", "sending"
                }:
                    existing_payload = json.loads(existing_event["payload_json"])
                    existing_payload["what_changed"] = timeline["what_changed"]
                    existing_payload["new_source"] = bool(
                        existing_payload.get("new_source")
                        or payload["new_source"]
                    )
                    conn.execute(
                        "UPDATE slack_outbox SET payload_json=? WHERE id=?",
                        (
                            json.dumps(existing_payload, ensure_ascii=False),
                            existing_event["id"],
                        ),
                    )
                    continue
                event_key = combined_key
                if existing_event:
                    # The coverage reply already reached Slack before this
                    # timeline row committed. Preserve the evolution as a
                    # separate reply instead of dropping it.
                    event_key = (
                        f"thread:story:{story['id']}:timeline:{timeline['id']}"
                    )
                    payload["new_source"] = False
                if _enqueue(
                    conn,
                    event_key=event_key,
                    event_type="thread_update",
                    entity_type="story",
                    entity_id=str(story["id"]),
                    story_id=str(story["id"]),
                    source_type=source,
                    channel_id=SLACK_CHANNEL_CANONICAL,
                    payload=payload,
                    parent_event_key=f"root:story:{story['id']}",
                    event_ts=_epoch(
                        mention["created_at"], _epoch(timeline["date"])
                    ),
                ):
                    counts["thread_updates"] += 1
                continue
            payload = {
                "title": story["title"],
                "summary": story["summary"],
                "url": story["primary_url"] or "",
                "source": "update",
                "sender": timeline["trigger_sender"] or "",
                "category": story["category"] or "",
                "date": timeline["date"],
                "new_source": False,
                "what_changed": timeline["what_changed"],
            }
            if _enqueue(
                conn,
                event_key=f"thread:story:{story['id']}:timeline:{timeline['id']}",
                event_type="thread_update",
                entity_type="story",
                entity_id=str(story["id"]),
                story_id=str(story["id"]),
                source_type="update",
                channel_id=SLACK_CHANNEL_CANONICAL,
                payload=payload,
                parent_event_key=f"root:story:{story['id']}",
                event_ts=_epoch(timeline["date"], int(time.time())),
            ):
                counts["thread_updates"] += 1
        if timelines:
            _set_state(conn, "timeline_checkpoint", timelines[-1]["id"])

        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_bookmarks'"
        ).fetchone():
            rows = conn.execute(
                """SELECT b.* FROM external_bookmarks b
                   WHERE NOT EXISTS (
                       SELECT 1 FROM slack_outbox o
                        WHERE o.event_key = 'root:external_bookmark:' || b.id
                   )
                   ORDER BY COALESCE(b.created_ts, b.scraped_at), b.id"""
            ).fetchall()
            for row in rows:
                inserted = _enqueue_external_bookmark(conn, row)
                counts["roots"] += min(inserted, 1)
                counts["source_posts"] += max(0, inserted - 1)

        if BOOKMARKS_DB_PATH.exists():
            bookmark_conn = sqlite3.connect(
                f"file:{BOOKMARKS_DB_PATH}?mode=ro", uri=True
            )
            bookmark_conn.row_factory = sqlite3.Row
            if bookmark_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raindrop_bookmarks'"
            ).fetchone():
                rows = bookmark_conn.execute(
                    "SELECT * FROM raindrop_bookmarks ORDER BY created_ts, id"
                ).fetchall()
                for row in rows:
                    key = f"root:raindrop_bookmark:{row['id']}"
                    exists = conn.execute(
                        "SELECT 1 FROM slack_outbox WHERE event_key=?", (key,)
                    ).fetchone()
                    if not exists:
                        inserted = _enqueue_raindrop_bookmark(conn, row)
                        counts["roots"] += min(inserted, 1)
                        counts["source_posts"] += max(0, inserted - 1)
            bookmark_conn.close()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def queue_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own_conn = conn is None
    conn = conn or _connect()
    statuses = {
        row[0]: int(row[1])
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM slack_outbox GROUP BY status"
        )
    }
    result = {
        "backfill_initialized": _state(conn, "backfill_initialized") == "1",
        "backfill_completed": _state(conn, "backfill_completed") == "1",
        "queue": statuses,
        "roots": int(conn.execute("SELECT COUNT(*) FROM slack_roots").fetchone()[0]),
        "blocked_errors": [
            {"error": row[0] or "unknown", "count": int(row[1])}
            for row in conn.execute(
                """SELECT last_error, COUNT(*) FROM slack_outbox
                   WHERE status='blocked'
                   GROUP BY last_error ORDER BY COUNT(*) DESC LIMIT 5"""
            )
        ],
    }
    if own_conn:
        conn.close()
    return result


def _ready_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT o.* FROM slack_outbox o
           WHERE o.status='pending'
             AND (o.available_at IS NULL OR o.available_at<=?)
             AND (
                 o.parent_event_key IS NULL OR EXISTS (
                     SELECT 1 FROM slack_roots r
                      WHERE r.entity_type=o.entity_type
                        AND r.entity_id=o.entity_id
                        AND COALESCE(r.permalink, '')!=''
                 )
             )
           ORDER BY o.event_ts ASC, o.id ASC
           LIMIT 500""",
        (now,),
    ).fetchall()
    selected: list[sqlite3.Row] = []
    channels: set[str] = set()
    for row in rows:
        if row["channel_id"] in channels:
            continue
        channels.add(row["channel_id"])
        selected.append(row)
    return selected


def _send_row(row: sqlite3.Row, client: SlackClient) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    permalink = ""
    thread_ts = None
    if row["parent_event_key"]:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        root = conn.execute(
            """SELECT root_ts, permalink FROM slack_roots
               WHERE entity_type=? AND entity_id=?""",
            (row["entity_type"], row["entity_id"]),
        ).fetchone()
        conn.close()
        if not root or not root["permalink"]:
            raise SlackAPIError("canonical_root_not_ready", retryable=True)
        permalink = root["permalink"]
        if row["event_type"] == "thread_update":
            thread_ts = root["root_ts"]

    text, blocks = build_message(row["event_type"], payload, permalink)
    client_msg_id = str(uuid.uuid5(uuid.NAMESPACE_URL, row["event_key"]))
    response = client.post_message(
        channel=row["channel_id"],
        text=text,
        blocks=blocks,
        client_msg_id=client_msg_id,
        thread_ts=thread_ts,
    )
    slack_ts = str(response.get("ts") or "")
    if not slack_ts:
        raise SlackAPIError("missing_message_ts", retryable=True)
    result = {"slack_ts": slack_ts, "permalink": "", "permalink_error": ""}
    if row["event_type"] == "canonical_root":
        try:
            result["permalink"] = client.get_permalink(
                channel=row["channel_id"], message_ts=slack_ts
            )
        except SlackAPIError as exc:
            result["permalink_error"] = str(exc)
    return result


def _repair_permalinks(conn: sqlite3.Connection, client: SlackClient) -> int:
    rows = conn.execute(
        """SELECT o.id, o.entity_type, o.entity_id, o.channel_id, o.slack_ts
           FROM slack_outbox o
           WHERE o.status='permalink_pending'
           ORDER BY o.id ASC LIMIT 20"""
    ).fetchall()
    repaired = 0
    for row in rows:
        try:
            permalink = client.get_permalink(
                channel=row["channel_id"], message_ts=row["slack_ts"]
            )
        except SlackAPIError as exc:
            status = "pending" if exc.retryable else "blocked"
            conn.execute(
                """UPDATE slack_outbox SET status=?, last_error=?,
                          available_at=? WHERE id=?""",
                (
                    "permalink_pending" if status == "pending" else "blocked",
                    f"permalink:{exc}",
                    (
                        datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after or 30)
                    ).isoformat(),
                    row["id"],
                ),
            )
            conn.commit()
            continue
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE slack_roots SET permalink=?
               WHERE entity_type=? AND entity_id=?""",
            (permalink, row["entity_type"], row["entity_id"]),
        )
        conn.execute(
            """UPDATE slack_outbox SET status='sent', sent_at=?,
                      last_error=NULL, available_at=NULL WHERE id=?""",
            (now, row["id"]),
        )
        conn.commit()
        repaired += 1
    return repaired


def drain(max_runtime: int = SLACK_WORKER_MAX_RUNTIME_SECONDS) -> dict[str, int]:
    if not SLACK_ENABLED:
        raise RuntimeError("SLACK_ENABLED is false")
    errors = configuration_errors(require_token=True)
    if errors:
        raise RuntimeError("; ".join(errors))
    conn = _connect()
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    conn.execute(
        """UPDATE slack_outbox SET status='pending', locked_at=NULL,
                  last_error='Recovered interrupted delivery'
           WHERE status='sending' AND COALESCE(locked_at, '')<?""",
        (stale_before,),
    )
    conn.commit()
    client = SlackClient()
    stats = {"sent": 0, "retried": 0, "blocked": 0, "permalinks": 0}
    started = time.monotonic()
    stats["permalinks"] += _repair_permalinks(conn, client)

    while time.monotonic() - started < max(1, max_runtime):
        rows = _ready_rows(conn)
        if not rows:
            break
        locked_at = datetime.now(timezone.utc).isoformat()
        ids = [int(row["id"]) for row in rows]
        conn.executemany(
            "UPDATE slack_outbox SET status='sending', locked_at=? WHERE id=? AND status='pending'",
            [(locked_at, row_id) for row_id in ids],
        )
        conn.commit()

        with ThreadPoolExecutor(max_workers=len(rows)) as executor:
            futures = {executor.submit(_send_row, row, client): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                now = datetime.now(timezone.utc)
                try:
                    result = future.result()
                except SlackAPIError as exc:
                    attempts = int(row["attempt_count"] or 0) + 1
                    if exc.retryable:
                        delay = exc.retry_after or min(3600, 2 ** min(attempts, 10))
                        conn.execute(
                            """UPDATE slack_outbox SET status='pending',
                                      attempt_count=?, available_at=?, last_error=?,
                                      locked_at=NULL WHERE id=?""",
                            (
                                attempts,
                                (now + timedelta(seconds=delay)).isoformat(),
                                str(exc),
                                row["id"],
                            ),
                        )
                        stats["retried"] += 1
                    else:
                        conn.execute(
                            """UPDATE slack_outbox SET status='blocked',
                                      attempt_count=?, last_error=?, locked_at=NULL
                               WHERE id=?""",
                            (attempts, str(exc), row["id"]),
                        )
                        stats["blocked"] += 1
                    conn.commit()
                    continue
                except Exception as exc:
                    conn.execute(
                        """UPDATE slack_outbox SET status='pending',
                                  attempt_count=attempt_count+1, available_at=?,
                                  last_error=?, locked_at=NULL WHERE id=?""",
                        (
                            (now + timedelta(seconds=30)).isoformat(),
                            f"worker_error:{exc.__class__.__name__}",
                            row["id"],
                        ),
                    )
                    conn.commit()
                    stats["retried"] += 1
                    continue

                status = "sent"
                last_error = None
                if row["event_type"] == "canonical_root":
                    conn.execute(
                        """INSERT INTO slack_roots
                           (entity_type, entity_id, channel_id, root_ts, permalink, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                             channel_id=excluded.channel_id,
                             root_ts=excluded.root_ts,
                             permalink=CASE
                               WHEN excluded.permalink!='' THEN excluded.permalink
                               ELSE slack_roots.permalink
                             END""",
                        (
                            row["entity_type"],
                            row["entity_id"],
                            row["channel_id"],
                            result["slack_ts"],
                            result["permalink"],
                            now.isoformat(),
                        ),
                    )
                    if not result["permalink"]:
                        status = "permalink_pending"
                        last_error = f"permalink:{result['permalink_error']}"
                conn.execute(
                    """UPDATE slack_outbox SET status=?, slack_ts=?, sent_at=?,
                              last_error=?, locked_at=NULL, available_at=NULL
                       WHERE id=?""",
                    (status, result["slack_ts"], now.isoformat(), last_error, row["id"]),
                )
                conn.commit()
                stats["sent"] += 1

        remaining = max_runtime - (time.monotonic() - started)
        if remaining <= 0:
            break
        time.sleep(min(PER_CHANNEL_INTERVAL_SECONDS, remaining))

    cutoff = _state(conn, "backfill_cutoff")
    pending_backfill = conn.execute(
        """SELECT COUNT(*) FROM slack_outbox
           WHERE event_type IN ('canonical_root', 'source_post')
             AND status!='sent'
             AND (?='' OR created_at<=?)""",
        (cutoff, cutoff),
    ).fetchone()[0]
    if int(pending_backfill) == 0:
        _set_state(conn, "backfill_completed", "1")
        conn.commit()
    conn.close()
    return stats


def run_worker() -> dict[str, Any]:
    if not SLACK_ENABLED:
        return {"enabled": False, "message": "Slack delivery disabled"}
    conn = _connect()
    initialized = _state(conn, "backfill_initialized") == "1"
    conn.close()
    if not initialized:
        raise RuntimeError(
            "Slack backfill is not initialized; run --preview-backfill and --init-backfill"
        )
    reconciled = reconcile()
    delivered = drain()
    status = queue_status()
    return {
        "enabled": True,
        "reconciled": reconciled,
        "delivered": delivered,
        "status": status,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--preview-backfill", action="store_true")
    parser.add_argument("--init-backfill", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--drain", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--retry-blocked", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--max-runtime",
        type=int,
        default=SLACK_WORKER_MAX_RUNTIME_SECONDS,
    )
    args = parser.parse_args()

    if args.validate:
        errors = configuration_errors(require_token=True)
        if errors:
            print("Slack configuration invalid: " + "; ".join(errors))
            raise SystemExit(1)
        print("Slack configuration valid")
        return
    if args.preview_backfill:
        print(json.dumps(preview_backfill(), indent=2, sort_keys=True))
        return
    if args.init_backfill:
        print(json.dumps(initialize_backfill(), indent=2, sort_keys=True))
        return
    if args.reconcile:
        print(json.dumps(reconcile(), indent=2, sort_keys=True))
        return
    if args.drain:
        print(json.dumps(drain(max_runtime=args.max_runtime), indent=2, sort_keys=True))
        return
    if args.status:
        print(json.dumps(queue_status(), indent=2, sort_keys=True))
        return
    if args.retry_blocked:
        print(json.dumps({"retried_blocked": retry_blocked()}, sort_keys=True))
        return

    result = run_worker()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

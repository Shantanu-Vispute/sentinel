import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from digest.slack_client import SlackAPIError
from digest import slack_delivery


CHANNELS = {
    "email": "C_EMAIL",
    "telegram": "C_TELEGRAM",
    "twitter": "C_TWITTER",
    "linkedin": "C_LINKEDIN",
    "youtube": "C_YOUTUBE",
    "raindrop": "C_RAINDROP",
}


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE stories (
            id TEXT PRIMARY KEY, title TEXT, summary TEXT, category TEXT,
            first_seen TEXT, last_updated TEXT, mention_count INTEGER,
            new_info_count INTEGER, is_read INTEGER, entities TEXT,
            primary_url TEXT, source_type TEXT
        );
        CREATE TABLE mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, story_id TEXT, title TEXT,
            summary TEXT, sender TEXT, gmail_url TEXT, date TEXT,
            created_at TEXT, added_new_info INTEGER DEFAULT 0,
            source_type TEXT, raw_title TEXT, raw_body TEXT
        );
        CREATE TABLE timeline_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, story_id TEXT, date TEXT,
            what_changed TEXT, trigger_sender TEXT, mention_id INTEGER
        );
        CREATE TABLE external_bookmarks (
            id TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT,
            excerpt TEXT, cover TEXT, author TEXT, created_ts INTEGER,
            scraped_at INTEGER, last_seen_at INTEGER
        );
        """
    )
    conn.execute(
        """INSERT INTO stories
           (id, title, summary, category, first_seen, last_updated,
            mention_count, new_info_count, is_read, entities, primary_url, source_type)
           VALUES ('story-1', 'Model launch', 'Initial summary', 'models',
                   '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                   1, 0, 0, '', 'https://example.com/story', 'email')"""
    )
    conn.execute(
        """INSERT INTO mentions
           (story_id, title, summary, sender, gmail_url, date, created_at, source_type)
           VALUES ('story-1', 'Model launch', 'Initial summary', 'newsletter',
                   'https://example.com/mention-1', '2026-08-01T00:00:00+00:00',
                   '2026-08-01T00:01:00+00:00', 'email')"""
    )
    conn.execute(
        """INSERT INTO external_bookmarks
           (id, source, url, title, excerpt, author, created_ts, scraped_at, last_seen_at)
           VALUES ('twitter:1', 'twitter', 'https://x.com/a/1', 'Saved post',
                   'Bookmark summary', 'author', 1785542400, 1785542400, 1785542400)"""
    )
    conn.commit()
    conn.close()


def _create_raindrop_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE raindrop_bookmarks (
            id TEXT PRIMARY KEY, title TEXT, note TEXT, excerpt TEXT,
            url TEXT, domain TEXT, folder TEXT, tags TEXT, created TEXT,
            created_ts INTEGER, last_update_ts INTEGER, cover TEXT,
            favorite INTEGER, synced_at INTEGER
        )"""
    )
    conn.execute(
        """INSERT INTO raindrop_bookmarks
           VALUES ('r-1', 'Paper', '', 'Visual RAG', 'https://example.com/paper',
                   'example.com', 'Research', '', '', 1785542500, 1785542500,
                   '', 0, 1785542500)"""
    )
    conn.commit()
    conn.close()


@contextmanager
def _configured(tmp_path: Path, *, enabled: bool = False):
    stories = tmp_path / "stories.db"
    bookmarks = tmp_path / "bookmarks.db"
    _create_db(stories)
    _create_raindrop_db(bookmarks)
    with patch.multiple(
        slack_delivery,
        DB_PATH=stories,
        BOOKMARKS_DB_PATH=bookmarks,
        SLACK_CHANNEL_CANONICAL="C_ALL",
        SLACK_CHANNELS=CHANNELS,
        SLACK_BOT_TOKEN="xoxb-test",
        SLACK_ENABLED=enabled,
    ):
        yield stories, bookmarks


def test_preview_and_root_only_backfill_are_complete_and_idempotent(tmp_path):
    with _configured(tmp_path):
        preview = slack_delivery.preview_backfill()
        assert preview == {
            "stories": 1,
            "external_bookmarks": {"twitter": 1},
            "raindrop_bookmarks": 1,
            "canonical_roots": 3,
            "source_posts": 3,
            "total_messages": 6,
        }

        inserted = slack_delivery.initialize_backfill()
        assert inserted == {
            "stories": 2,
            "external_bookmarks": 2,
            "raindrop_bookmarks": 2,
        }

        conn = slack_delivery._connect()
        assert conn.execute("SELECT COUNT(*) FROM slack_outbox").fetchone()[0] == 6
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_outbox WHERE event_type='thread_update'"
        ).fetchone()[0] == 0
        assert slack_delivery._state(
            conn, "historical_mentions_suppressed_through"
        ) == "1"
        assert slack_delivery._state(conn, "backfill_completed") == "0"
        canonical_times = [
            row[0]
            for row in conn.execute(
                """SELECT event_ts FROM slack_outbox
                   WHERE event_type='canonical_root'
                   ORDER BY event_ts ASC, id ASC"""
            )
        ]
        assert canonical_times == sorted(canonical_times)
        conn.close()


def test_reconcile_posts_every_mention_but_threads_only_new_source_or_evolution(tmp_path):
    with _configured(tmp_path):
        slack_delivery.initialize_backfill()
        conn = sqlite3.connect(slack_delivery.DB_PATH)
        same_source_id = conn.execute(
            """INSERT INTO mentions
               (story_id, title, summary, sender, gmail_url, date, created_at, source_type)
               VALUES ('story-1', 'Same coverage', 'No change', 'newsletter-2',
                       'https://example.com/mention-2', '2026-08-02T00:00:00+00:00',
                       '2026-08-02T00:01:00+00:00', 'email')"""
        ).lastrowid
        conn.commit()
        conn.close()

        result = slack_delivery.reconcile()
        assert result["source_posts"] == 1
        assert result["thread_updates"] == 0

        conn = sqlite3.connect(slack_delivery.DB_PATH)
        telegram_id = conn.execute(
            """INSERT INTO mentions
               (story_id, title, summary, sender, gmail_url, date, created_at,
                added_new_info, source_type)
               VALUES ('story-1', 'Telegram coverage', 'New benchmark', '@channel',
                       'https://t.me/channel/2', '2026-08-03T00:00:00+00:00',
                       '2026-08-03T00:01:00+00:00', 1, 'telegram')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO timeline_entries
               (story_id, date, what_changed, trigger_sender, mention_id)
               VALUES ('story-1', '2026-08-03T00:00:00+00:00',
                       'Benchmark results were added.', '@channel', ?)""",
            (telegram_id,),
        )
        conn.commit()
        conn.close()

        result = slack_delivery.reconcile()
        assert result["source_posts"] == 1
        assert result["thread_updates"] == 1

        conn = slack_delivery._connect()
        row = conn.execute(
            "SELECT payload_json FROM slack_outbox WHERE event_key=?",
            (f"thread:story:story-1:mention:{telegram_id}",),
        ).fetchone()
        payload = json.loads(row[0])
        assert payload["new_source"] is True
        assert payload["what_changed"] == "Benchmark results were added."
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_outbox WHERE event_key=?",
            (f"source:story:story-1:mention:{same_source_id}",),
        ).fetchone()[0] == 1
        conn.close()


def test_source_posts_wait_for_canonical_permalink(tmp_path):
    with _configured(tmp_path):
        slack_delivery.initialize_backfill()
        conn = slack_delivery._connect()
        ready = slack_delivery._ready_rows(conn)
        assert {row["event_type"] for row in ready} == {"canonical_root"}

        conn.execute(
            """INSERT INTO slack_roots
               (entity_type, entity_id, channel_id, root_ts, permalink, created_at)
               VALUES ('story', 'story-1', 'C_ALL', '1.0',
                       'https://workspace.slack.com/story-1', '2026-08-01')"""
        )
        conn.execute(
            "UPDATE slack_outbox SET status='sent' WHERE event_key='root:story:story-1'"
        )
        conn.commit()
        ready = slack_delivery._ready_rows(conn)
        assert any(
            row["event_key"].startswith("source:story:story-1") for row in ready
        )
        conn.close()


def test_late_timeline_commit_still_creates_evolution_reply(tmp_path):
    with _configured(tmp_path):
        slack_delivery.initialize_backfill()
        conn = sqlite3.connect(slack_delivery.DB_PATH)
        mention_id = conn.execute(
            """INSERT INTO mentions
               (story_id, title, summary, sender, gmail_url, date, created_at,
                added_new_info, source_type)
               VALUES ('story-1', 'Later detail', 'Summary', 'newsletter-2',
                       'https://example.com/later', '2026-08-04T00:00:00+00:00',
                       '2026-08-04T00:01:00+00:00', 1, 'email')"""
        ).lastrowid
        conn.commit()
        conn.close()

        first = slack_delivery.reconcile()
        assert first["thread_updates"] == 0

        conn = sqlite3.connect(slack_delivery.DB_PATH)
        conn.execute(
            """INSERT INTO timeline_entries
               (story_id, date, what_changed, trigger_sender, mention_id)
               VALUES ('story-1', '2026-08-04T00:00:00+00:00',
                       'A late timeline update.', 'newsletter-2', ?)""",
            (mention_id,),
        )
        conn.commit()
        conn.close()

        second = slack_delivery.reconcile()
        assert second["thread_updates"] == 1
        conn = slack_delivery._connect()
        row = conn.execute(
            "SELECT payload_json FROM slack_outbox WHERE event_key=?",
            (f"thread:story:story-1:mention:{mention_id}",),
        ).fetchone()
        assert json.loads(row[0])["what_changed"] == "A late timeline update."
        conn.close()


def test_message_builder_keeps_root_immutable_and_thread_quiet_data():
    payload = {
        "title": "Model <launch>",
        "summary": "Summary",
        "url": "https://example.com",
        "source": "telegram",
        "sender": "@channel",
        "category": "models",
        "date": "2026-08-03T00:00:00+00:00",
        "new_source": True,
        "what_changed": "A benchmark was added.",
    }
    text, blocks = slack_delivery.build_message("thread_update", payload)
    assert "A benchmark was added." in text
    assert "New source: telegram" in blocks[0]["text"]["text"]
    assert "\n>A benchmark was added." in blocks[0]["text"]["text"]
    assert "reply_broadcast" not in json.dumps(blocks)


def test_message_builder_formats_summary_as_blockquote():
    payload = {
        "title": "A story",
        "summary": "A short summary.",
        "url": "https://example.com",
        "source": "telegram",
        "date": "2026-08-03T00:00:00+00:00",
    }
    _, blocks = slack_delivery.build_message("canonical_root", payload)
    assert "\n>A short summary." in blocks[0]["text"]["text"]


def test_message_builder_uses_compact_link_labels():
    payload = {
        "title": "A story",
        "summary": "A short summary.",
        "url": "https://example.com/source",
        "source": "telegram",
        "date": "2026-08-03T00:00:00+00:00",
        "what_changed": "A new detail appeared.",
    }
    _, source_blocks = slack_delivery.build_message(
        "source_post", payload, "https://example.com/thread"
    )
    _, update_blocks = slack_delivery.build_message("thread_update", payload)
    assert "View merged timeline" not in source_blocks[0]["text"]["text"]
    assert source_blocks[2] == {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "View merged timeline"},
                "url": "https://example.com/thread",
                "action_id": "view_merged_timeline",
            }
        ],
    }
    assert "Open canonical thread" not in json.dumps(source_blocks)
    assert "View new coverage" in update_blocks[0]["text"]["text"]


def test_retryable_and_permanent_delivery_errors_keep_queue_state(tmp_path):
    with _configured(tmp_path, enabled=True):
        slack_delivery.initialize_backfill()
        with patch(
            "digest.slack_delivery._send_row",
            side_effect=SlackAPIError("ratelimited", retryable=True, retry_after=30),
        ), patch("digest.slack_delivery.time.sleep", return_value=None):
            stats = slack_delivery.drain(max_runtime=1)
        assert stats["retried"] > 0
        conn = slack_delivery._connect()
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_outbox WHERE status='pending' AND last_error='ratelimited'"
        ).fetchone()[0] > 0
        conn.close()

    other = tmp_path / "blocked"
    other.mkdir()
    with _configured(other, enabled=True):
        slack_delivery.initialize_backfill()
        with patch(
            "digest.slack_delivery._send_row",
            side_effect=SlackAPIError("invalid_auth", retryable=False),
        ), patch("digest.slack_delivery.time.sleep", return_value=None):
            stats = slack_delivery.drain(max_runtime=1)
        assert stats["blocked"] > 0
        conn = slack_delivery._connect()
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_outbox WHERE status='blocked' AND last_error='invalid_auth'"
        ).fetchone()[0] > 0
        conn.close()

        status = slack_delivery.queue_status()
        assert status["blocked_errors"][0]["error"] == "invalid_auth"

        assert slack_delivery.retry_blocked() > 0
        conn = slack_delivery._connect()
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_outbox WHERE status='blocked'"
        ).fetchone()[0] == 0
        conn.close()

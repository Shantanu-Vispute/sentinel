import sqlite3
import chromadb
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional
from config import STORIES_DB

STATE_DIR = Path(__file__).resolve().parent.parent / "state"
STATE_DIR.mkdir(exist_ok=True)

DB_PATH = Path(STORIES_DB)
CHROMA_PATH = STATE_DIR / "chroma"

class StoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

        self.chroma = chromadb.PersistentClient(path=str(CHROMA_PATH))
        self.collection = self.chroma.get_or_create_collection(
            "stories", metadata={"hnsw:space": "cosine"}
        )

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS stories (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                summary      TEXT NOT NULL,
                entities     TEXT,
                first_seen   TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                mention_count  INTEGER DEFAULT 0,
                new_info_count INTEGER DEFAULT 0,
                is_read        INTEGER DEFAULT 0,
                category       TEXT
            );

            CREATE TABLE IF NOT EXISTS mentions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id       TEXT NOT NULL,
                title          TEXT,
                summary        TEXT,
                raw_title      TEXT,
                raw_body       TEXT,
                sender         TEXT NOT NULL,
                gmail_url      TEXT NOT NULL,
                date           TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                added_new_info INTEGER DEFAULT 0,
                FOREIGN KEY (story_id) REFERENCES stories(id)
            );

            CREATE TABLE IF NOT EXISTS timeline_entries (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id       TEXT NOT NULL,
                date           TEXT NOT NULL,
                what_changed   TEXT NOT NULL,
                trigger_sender TEXT,
                FOREIGN KEY (story_id) REFERENCES stories(id)
            );

            CREATE TABLE IF NOT EXISTS processed_emails (
                gmail_message_id TEXT PRIMARY KEY,
                processed_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS story_x_links (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id       TEXT NOT NULL,
                tweet_id       TEXT,
                url            TEXT NOT NULL,
                discovered_via TEXT NOT NULL,
                discovered_at  TEXT NOT NULL,
                FOREIGN KEY (story_id) REFERENCES stories(id),
                UNIQUE (story_id, url)
            );

            CREATE INDEX IF NOT EXISTS idx_stories_updated
                ON stories(last_updated DESC);
            CREATE INDEX IF NOT EXISTS idx_mentions_story
                ON mentions(story_id);
            CREATE INDEX IF NOT EXISTS idx_timeline_story
                ON timeline_entries(story_id);
            CREATE INDEX IF NOT EXISTS idx_story_x_links_story
                ON story_x_links(story_id);
        """)

        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(stories)")}
        if "category" not in cols:
            self.conn.execute("ALTER TABLE stories ADD COLUMN category TEXT")
        if "primary_url" not in cols:
            self.conn.execute(
                "ALTER TABLE stories ADD COLUMN primary_url TEXT")
        if "primary_url_host" not in cols:
            self.conn.execute(
                "ALTER TABLE stories ADD COLUMN primary_url_host TEXT")
        if "source_type" not in cols:
            self.conn.execute(
                "ALTER TABLE stories ADD COLUMN source_type TEXT DEFAULT 'email'")
        if "primary_image_url" not in cols:
            self.conn.execute(
                "ALTER TABLE stories ADD COLUMN primary_image_url TEXT")
        if "links_json" not in cols:
            self.conn.execute("ALTER TABLE stories ADD COLUMN links_json TEXT")
        if "content_hash" not in cols:
            self.conn.execute(
                "ALTER TABLE stories ADD COLUMN content_hash TEXT")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stories_content_hash ON stories(content_hash)"
            )
        if "x_links_scanned_at" not in cols:
            self.conn.execute(
                "ALTER TABLE stories ADD COLUMN x_links_scanned_at TEXT")
        mcols = {r[1]
                 for r in self.conn.execute("PRAGMA table_info(mentions)")}
        if "source_type" not in mcols:
            self.conn.execute(
                "ALTER TABLE mentions ADD COLUMN source_type TEXT DEFAULT 'email'")
        if "raw_title" not in mcols:
            self.conn.execute("ALTER TABLE mentions ADD COLUMN raw_title TEXT")
        if "raw_body" not in mcols:
            self.conn.execute("ALTER TABLE mentions ADD COLUMN raw_body TEXT")
        self.conn.commit()

    def find_similar(
            self,
            embedding: list[float],
            threshold: float = 0.85) -> Optional[str]:
        results = self.collection.query(
            query_embeddings=[embedding], n_results=1)
        if results and results["ids"] and results["ids"][0]:
            similarity = 1 - results["distances"][0][0]
            if similarity >= threshold:
                return results["ids"][0][0]
        return None

    def find_by_content_hash(self, content_hash: str) -> Optional[str]:
        if not content_hash:
            return None
        row = self.conn.execute(
            "SELECT id FROM stories WHERE content_hash = ? LIMIT 1",
            (content_hash,),
        ).fetchone()
        return row["id"] if row else None

    def add_story(
        self,
        title: str,
        summary: str,
        entities: list[str],
        embedding: list[float] | None,
        sender: str,
        gmail_url: str,
        date: str,
        mention_title: str = "",
        mention_summary: str = "",
        mention_raw_title: str = "",
        mention_raw_body: str = "",
        category: str = "other",
        primary_url: str = "",
        source_type: str = "email",
        primary_image_url: str = "",
        links: list[str] | None = None,
        content_hash: str = "",
    ) -> str:
        story_id = str(uuid.uuid4())
        now = datetime.now().isoformat()

        host = ""
        if primary_url:
            try:
                from urllib.parse import urlsplit
                host = urlsplit(primary_url).netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
            except Exception:
                host = ""

        import json as _json
        links_payload = _json.dumps(links) if links else None
        self.conn.execute(
            """INSERT INTO stories
               (id, title, summary, entities, first_seen, last_updated,
                mention_count, category, primary_url, primary_url_host,
                source_type, primary_image_url, links_json, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (story_id, title, summary, ",".join(entities), now, now,
             category, primary_url, host, source_type, primary_image_url,
             links_payload, content_hash or None),
        )
        self.conn.execute(
            """INSERT INTO mentions
               (story_id, title, summary, raw_title, raw_body, sender, gmail_url, date, created_at, source_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (story_id, mention_title or title, mention_summary or summary,
             mention_raw_title or mention_title or title,
             mention_raw_body or mention_summary or summary,
             sender, gmail_url, date, now, source_type),
        )
        self.conn.commit()

        if embedding is not None:
            self.collection.add(
                ids=[story_id],
                embeddings=[embedding],
                metadatas=[{"title": title, "date": date}],
            )
        return story_id

    def add_mention(
        self,
        story_id: str,
        title: str,
        summary: str,
        sender: str,
        gmail_url: str,
        date: str,
        added_new_info: bool = False,
        source_type: str = "email",
        raw_title: str = "",
        raw_body: str = "",
    ):
        now = datetime.now().isoformat()
        self.conn.execute(
            """INSERT INTO mentions
               (story_id, title, summary, raw_title, raw_body, sender, gmail_url, date, created_at, added_new_info, source_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (story_id, title, summary, raw_title or title, raw_body or summary, sender, gmail_url, date, now, int(added_new_info), source_type),
        )
        self.conn.execute(
            "UPDATE stories SET mention_count = mention_count + 1, last_updated = ? WHERE id = ?",
            (now, story_id),
        )
        self.conn.commit()

    def update_story_summary(self, story_id: str, new_summary: str):
        now = datetime.now().isoformat()
        self.conn.execute(
            "UPDATE stories SET summary = ?, last_updated = ? WHERE id = ?",
            (new_summary, now, story_id),
        )
        self.conn.commit()

    def update_story_title(self, story_id: str, new_title: str):
        self.conn.execute(
            "UPDATE stories SET title = ? WHERE id = ?",
            (new_title, story_id),
        )
        self.conn.commit()

    def update_story_image(self, story_id: str, primary_image_url: str):
        self.conn.execute(
            "UPDATE stories SET primary_image_url = ? WHERE id = ?",
            (primary_image_url, story_id),
        )
        self.conn.commit()

    def add_timeline_entry(
            self,
            story_id: str,
            date: str,
            what_changed: str,
            trigger_sender: str = ""):
        self.conn.execute(
            """INSERT INTO timeline_entries (story_id, date, what_changed, trigger_sender)
               VALUES (?, ?, ?, ?)""", (story_id, date, what_changed, trigger_sender), )
        self.conn.commit()

    def increment_new_info_count(self, story_id: str):
        self.conn.execute(
            "UPDATE stories SET new_info_count = new_info_count + 1 WHERE id = ?",
            (story_id,),
        )
        self.conn.commit()

    def mark_unread(self, story_id: str):
        self.conn.execute(
            "UPDATE stories SET is_read = 0 WHERE id = ?", (story_id,))
        self.conn.commit()

    def add_embedding(self, story_id: str, embedding: list[float], title: str, date: str):
        """Add an embedding for a story that already exists in `stories` but
        was never added to the vector collection (e.g. an old Telegram story
        ingested before embeddings were wired up for that source)."""
        self.collection.add(
            ids=[story_id],
            embeddings=[embedding],
            metadatas=[{"title": title, "date": date}],
        )

    def merge_stories(self, source_id: str, target_id: str):
        """Merge `source_id` into `target_id`: move its mentions/timeline
        entries over, fold its mention_count into the target, then delete
        the now-empty source row. Used for retroactively fixing pre-existing
        duplicate stories (e.g. an old Telegram story that turns out to
        describe the same event as an existing email story)."""
        source = self.get_story(source_id)
        if source is None:
            return
        self.conn.execute(
            "UPDATE mentions SET story_id = ? WHERE story_id = ?", (target_id, source_id),
        )
        self.conn.execute(
            "UPDATE timeline_entries SET story_id = ? WHERE story_id = ?", (target_id, source_id),
        )
        self.conn.execute(
            "UPDATE stories SET mention_count = mention_count + ? WHERE id = ?",
            (source["mention_count"] or 0, target_id),
        )
        self.conn.execute("DELETE FROM stories WHERE id = ?", (source_id,))
        self.conn.commit()

    def get_story(self, story_id: str) -> Optional[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM stories WHERE id = ?", (story_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def stories_needing_x_link_scan(self, days: int | None = None, limit: int = 50) -> list[dict]:
        query = """SELECT id, title, primary_url, COALESCE(links_json, '') AS links_json
                     FROM stories
                    WHERE x_links_scanned_at IS NULL"""
        params: list = []
        if days is not None:
            query += " AND first_seen >= datetime('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY first_seen ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def mark_x_links_scanned(self, story_id: str):
        self.conn.execute(
            "UPDATE stories SET x_links_scanned_at = ? WHERE id = ?",
            (datetime.now().isoformat(), story_id),
        )
        self.conn.commit()

    def add_x_links(self, story_id: str, links: list[dict], discovered_via: str):
        now = datetime.now().isoformat()
        for link in links:
            self.conn.execute(
                """INSERT OR IGNORE INTO story_x_links
                   (story_id, tweet_id, url, discovered_via, discovered_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (story_id, link.get("tweet_id", ""), link["url"], discovered_via, now),
            )
        self.conn.commit()

    def get_stats(self) -> dict:
        total_stories = self.conn.execute(
            "SELECT COUNT(*) FROM stories").fetchone()[0]
        total_mentions = self.conn.execute(
            "SELECT COUNT(*) FROM mentions").fetchone()[0]
        unread = self.conn.execute(
            "SELECT COUNT(*) FROM stories WHERE is_read = 0 AND last_updated > datetime('now', '-30 days')"
        ).fetchone()[0]
        evolved = self.conn.execute(
            "SELECT COUNT(*) FROM stories WHERE new_info_count > 0"
        ).fetchone()[0]
        return {
            "total_stories": total_stories,
            "total_mentions": total_mentions,
            "unread": unread,
            "evolved": evolved,
        }

    def is_email_processed(self, gmail_message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_emails WHERE gmail_message_id = ?",
            (gmail_message_id,)
        ).fetchone()
        return row is not None

    def mark_email_processed(self, gmail_message_id: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_emails (gmail_message_id, processed_at) VALUES (?, ?)",
            (gmail_message_id, datetime.utcnow().isoformat())
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

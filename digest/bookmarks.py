"""Read the locally migrated Raindrop bookmark library for the Flask UI."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config import BOOKMARKS_DB


BOOKMARKS_DB_PATH = Path(BOOKMARKS_DB)


def _connect() -> sqlite3.Connection | None:
    if not BOOKMARKS_DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{BOOKMARKS_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def list_bookmarks(page: int = 1, per_page: int = 24) -> tuple[list[dict], int]:
    """Return the bookmark cards used by the unified dashboard."""
    conn = _connect()
    if conn is None:
        return [], 0
    try:
        if not _has_table(conn, "raindrop_bookmarks"):
            return [], 0
        has_state = _has_table(conn, "bookmark_state")
        state_join = (
            "LEFT JOIN bookmark_state s ON s.id = b.id" if has_state else ""
        )
        title = "COALESCE(NULLIF(s.title_override, ''), b.title)" if has_state else "b.title"
        status = "COALESCE(s.status, 'inbox')" if has_state else "'inbox'"
        total = conn.execute("SELECT COUNT(*) FROM raindrop_bookmarks").fetchone()[0]
        offset = (max(page, 1) - 1) * max(per_page, 1)
        rows = conn.execute(
            f"""SELECT b.id, {title} AS title, b.excerpt, b.url, b.domain,
                       b.folder, b.tags, b.cover, b.favorite, b.created,
                       {status} AS status
                  FROM raindrop_bookmarks b
                  {state_join}
              ORDER BY b.last_update_ts DESC, b.created_ts DESC
                 LIMIT ? OFFSET ?""",
            (per_page, offset),
        ).fetchall()
        return [dict(row) for row in rows], int(total)
    finally:
        conn.close()

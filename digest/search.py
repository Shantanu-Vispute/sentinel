"""Search helpers for lexical, semantic, and hybrid story retrieval."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable


def build_fts_query(query: str) -> str:
    """Convert free text into a safe AND query for SQLite FTS5."""
    tokens = re.findall(r"[\w]+", query or "", flags=re.UNICODE)
    return " AND ".join(f'"{token}"*' for token in tokens)


def lexical_rankings(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 2000,
) -> list[str]:
    """Return story IDs ordered by SQLite FTS5 BM25 rank."""
    match_query = build_fts_query(query)
    if not match_query:
        return []
    try:
        rows = conn.execute(
            """SELECT story_id
                 FROM stories_fts
                WHERE stories_fts MATCH ?
                ORDER BY bm25(stories_fts)
                LIMIT ?""",
            (match_query, max(1, limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row["story_id"] if isinstance(row, sqlite3.Row) else row[0] for row in rows]


def reciprocal_rank_fusion(
    lexical_ids: Iterable[str],
    semantic_ids: Iterable[str],
    *,
    lexical_weight: float = 0.55,
    semantic_weight: float = 0.45,
    rank_constant: int = 60,
) -> dict[str, float]:
    """Combine two ranked ID lists into one score map."""
    scores: dict[str, float] = {}
    for rank, story_id in enumerate(lexical_ids, start=1):
        scores[story_id] = scores.get(story_id, 0.0) + lexical_weight / (rank_constant + rank)
    for rank, story_id in enumerate(semantic_ids, start=1):
        scores[story_id] = scores.get(story_id, 0.0) + semantic_weight / (rank_constant + rank)
    return scores

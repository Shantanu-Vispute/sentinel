import json
import os
import pathlib
import re
import sqlite3
import time
from datetime import datetime

from flask import Flask, jsonify, redirect, render_template, request
from config import STORIES_DB

HERE = pathlib.Path(__file__).resolve().parent
STORIES_DB_PATH = pathlib.Path(STORIES_DB)
STORY_DETAIL_COLUMNS = """id, title, summary, first_seen, last_updated,
mention_count, new_info_count, is_read, primary_url, primary_url_host,
COALESCE(skipped, 0) AS skipped,
COALESCE(source_type, 'email') AS source_type,
COALESCE(primary_image_url, '') AS primary_image_url,
COALESCE(links_json, '') AS links_json,
COALESCE(content_hash, '') AS content_hash"""

app = Flask(__name__)

@app.template_filter("hash_int")
def _hash_int(s: str) -> int:
    import hashlib

    return int(hashlib.md5((s or "").encode("utf-8")).hexdigest()[:8], 16)

def _stories_conn(readonly: bool = True) -> sqlite3.Connection | None:
    if not STORIES_DB_PATH.exists():
        return None
    if readonly:
        uri = f"file:{STORIES_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(STORIES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _parse_links_json(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        from urllib.parse import urlsplit

        urls = json.loads(raw)
        out = []
        for url in urls:
            if not isinstance(url, str):
                continue
            host = urlsplit(url).netloc.lower().removeprefix("www.")
            if host:
                out.append({"url": url, "host": host})
        return out
    except Exception:
        return []

def _parse_iso_ts(s: str) -> int:
    if not s:
        return 0
    try:
        return int(
            datetime.fromisoformat(
                s.replace(
                    "Z",
                    "+00:00")).timestamp())
    except Exception:
        return 0

def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        host = urlsplit(url).netloc.lower()
        return host.removeprefix("www.")
    except Exception:
        return ""

def _iso_from_ts(ts: int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).isoformat()
    except Exception:
        return ""

def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None

def _ensure_external_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS external_item_state (
            id TEXT PRIMARY KEY,
            is_read INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )

def _external_row_to_story(row: sqlite3.Row) -> dict:
    source = row["source"] or "external"
    created_ts = row["created_ts"] or row["last_seen_at"] or row["scraped_at"] or 0
    updated_ts = row["last_seen_at"] or row["scraped_at"] or created_ts
    primary_url = row["url"] or ""
    author = row["author"] or ""
    links = []
    link_card_raw = row["link_card_json"] if "link_card_json" in row.keys(
    ) else ""
    if link_card_raw:
        try:
            card = json.loads(link_card_raw)
            if card and card.get("url"):
                links.append(
                    {"url": card["url"], "host": _host_of(card["url"])})
        except Exception:
            pass
    return {
        "id": row["id"],
        "title": row["title"] or primary_url or "(untitled)",
        "summary": row["excerpt"] or "",
        "primary_url": primary_url,
        "primary_url_host": _host_of(primary_url),
        "mention_count": 1,
        "new_info_count": 0,
        "is_read": bool(row["is_read"] or 0),
        "first_seen": _iso_from_ts(created_ts),
        "last_updated": _iso_from_ts(updated_ts),
        "age_days": None,
        "skipped": bool(row["skipped"] or 0),
        "source_type": source,
        "source_label": _source_label(source),
        "primary_image_url": row["cover"] or "",
        "links": links,
        "senders": [author] if author else [source],
        "author": author,
    }

def _load_external_stories(conn: sqlite3.Connection) -> list[dict]:
    if not _has_table(conn, "external_bookmarks"):
        return []
    has_state = _has_table(conn, "external_item_state")
    if has_state:
        rows = conn.execute(
            """SELECT b.*, COALESCE(s.is_read, 0) AS is_read,
                      COALESCE(s.skipped, 0) AS skipped
                 FROM external_bookmarks b
                 LEFT JOIN external_item_state s ON s.id = b.id"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT b.*, 0 AS is_read, 0 AS skipped
                 FROM external_bookmarks b"""
        ).fetchall()
    return [_external_row_to_story(row) for row in rows]

def _get_external_story(
        conn: sqlite3.Connection,
        story_id: str) -> dict | None:
    if not _has_table(conn, "external_bookmarks"):
        return None
    has_state = _has_table(conn, "external_item_state")
    if has_state:
        row = conn.execute(
            """SELECT b.*, COALESCE(s.is_read, 0) AS is_read,
                      COALESCE(s.skipped, 0) AS skipped
                 FROM external_bookmarks b
                 LEFT JOIN external_item_state s ON s.id = b.id
                WHERE b.id=?""",
            (story_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT b.*, 0 AS is_read, 0 AS skipped FROM external_bookmarks b WHERE b.id=?",
            (story_id,),
        ).fetchone()
    return _external_row_to_story(row) if row else None

def _mark_external_state(
        story_id: str,
        *,
        is_read: int | None = None,
        skipped: int | None = None) -> None:
    conn = _stories_conn(readonly=False)
    if conn is None:
        return
    now = datetime.now().isoformat()
    _ensure_external_state_table(conn)
    existing = conn.execute(
        "SELECT is_read, skipped FROM external_item_state WHERE id=?",
        (story_id,),
    ).fetchone()
    next_read = int(is_read) if is_read is not None else int(
        existing["is_read"]) if existing else 0
    next_skipped = int(skipped) if skipped is not None else int(
        existing["skipped"]) if existing else 0
    conn.execute(
        """INSERT INTO external_item_state (id, is_read, skipped, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             is_read=excluded.is_read,
             skipped=excluded.skipped,
             updated_at=excluded.updated_at""",
        (story_id, next_read, next_skipped, now),
    )
    conn.commit()
    conn.close()

TOKEN_RE = re.compile(r'(-)?([a-zA-Z]+:)?("([^"]+)"|\S+)')
AGE_RE = re.compile(
    r"^(<|>|<=|>=)?\s*(\d+)\s*(d|day|days|w|wk|weeks|mo|month|months|y|yr|year|years)$",
    re.I,
)
_UNIT_DAYS = {
    "d": 1,
    "day": 1,
    "days": 1,
    "w": 7,
    "wk": 7,
    "weeks": 7,
    "mo": 30,
    "month": 30,
    "months": 30,
    "y": 365,
    "yr": 365,
    "year": 365,
    "years": 365,
}

def _parse_age(expr: str):
    m = AGE_RE.match(expr.strip())
    if not m:
        return None
    op, num, unit = m.group(1) or "=", int(m.group(2)), m.group(3).lower()
    return op, num * _UNIT_DAYS[unit]

def parse_clauses(q: str) -> list[dict]:
    clauses = []
    for m in TOKEN_RE.finditer(q):
        neg = bool(m.group(1))
        op = (m.group(2) or "").rstrip(":").lower()
        raw = m.group(4) if m.group(4) else m.group(3).strip('"')
        if raw:
            clauses.append({"op": op, "val": raw, "neg": neg})
    return clauses

def _match_story(
        story: dict,
        clauses: list[dict],
        senders_for_story: list[str]) -> bool:
    title = (story.get("title") or "").lower()
    summary = (story.get("summary") or "").lower()
    source_type = (story.get("source_type") or "").lower()
    primary_host = (story.get("primary_url_host") or "").lower()
    senders_lower = [s.lower() for s in senders_for_story]
    age_days = story.get("age_days")

    for clause in clauses:
        val = (clause.get("val") or "").lower()
        op = clause.get("op") or ""
        ok = False
        if op == "":
            pattern = r"(?<![a-z0-9])" + re.escape(val) + r"(?![a-z0-9])"
            ok = bool(re.search(pattern, title) or re.search(pattern, summary))
        elif op == "sender":
            ok = any(val in s for s in senders_lower)
        elif op == "source":
            ok = source_type == val
        elif op in {"site", "host", "domain"}:
            ok = val in primary_host
        elif op == "is":
            if val == "read":
                ok = bool(story.get("is_read"))
            elif val == "unread":
                ok = not story.get("is_read")
            elif val == "skipped":
                ok = bool(story.get("skipped"))
        elif op == "age":
            parsed = _parse_age(clause["val"])
            if parsed and age_days is not None:
                cmp_op, days = parsed
                ok = (
                    (cmp_op == ">" and age_days > days)
                    or (cmp_op == ">=" and age_days >= days)
                    or (cmp_op == "<" and age_days < days)
                    or (cmp_op == "<=" and age_days <= days)
                    or (cmp_op == "=" and age_days == days)
                )
        else:
            ok = val in title or val in summary
        if clause.get("neg"):
            ok = not ok
        if not ok:
            return False
    return True

def _source_label(source_type: str) -> str:
    labels = {
        "email": "Email",
        "telegram": "Telegram",
        "twitter": "Twitter",
        "linkedin": "LinkedIn",
        "youtube": "YouTube",
    }
    return labels.get(source_type, source_type.replace("_", " ").title())

@app.route("/")
def index():
    return redirect("/digest")

@app.route("/digest")
def digest_list():
    conn = _stories_conn()
    if conn is None:
        return render_template(
            "digest_list.html",
            stories=[],
            total=0,
            total_all=0,
            missing=True,
            source_counts=[],
            q="",
            sort="recent",
            state="",
            source="all",
        )

    rows = conn.execute(
        """SELECT id, title, summary, first_seen, last_updated,
                  mention_count, new_info_count, is_read,
                  primary_url, primary_url_host,
                  COALESCE(skipped, 0) AS skipped,
                  COALESCE(source_type, 'email') AS source_type,
                  COALESCE(primary_image_url, '') AS primary_image_url,
                  COALESCE(links_json, '') AS links_json
             FROM stories
            ORDER BY last_updated DESC
            LIMIT 2000"""
    ).fetchall()
    sender_rows = conn.execute(
        "SELECT story_id, sender FROM mentions").fetchall()
    external_stories = _load_external_stories(conn)
    conn.close()

    senders_by_story: dict[str, list[str]] = {}
    for row in sender_rows:
        senders_by_story.setdefault(
            row["story_id"],
            []).append(
            row["sender"] or "")

    now_t = int(time.time())
    q = (request.args.get("q") or "").strip()

    sort = (request.args.get("sort") or "recent").strip().lower()
    state = (request.args.get("state") or "").strip().lower()
    source = (request.args.get("source") or "all").strip().lower()

    all_stories = []
    source_count_map: dict[str, int] = {}

    for row in rows:
        ts = _parse_iso_ts(row["last_updated"])
        age_days = (now_t - ts) // 86400 if ts else None
        story = {
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"] or "",
            "primary_url": row["primary_url"] or "",
            "primary_url_host": row["primary_url_host"] or "",
            "mention_count": row["mention_count"] or 0,
            "new_info_count": row["new_info_count"] or 0,
            "is_read": bool(row["is_read"]),
            "last_updated": row["last_updated"],
            "first_seen": row["first_seen"],
            "age_days": age_days,
            "skipped": bool(row["skipped"]),
            "source_type": row["source_type"] or "email",
            "source_label": _source_label(row["source_type"] or "email"),
            "primary_image_url": row["primary_image_url"] or "",
            "links": _parse_links_json(row["links_json"]),
            "senders": [],
        }

        seen_senders = set()
        unique_senders = []
        for sender in senders_by_story.get(row["id"], []):
            key = (sender or "").strip()
            if key and key.lower() not in seen_senders:
                seen_senders.add(key.lower())
                unique_senders.append(key)
        story["senders"] = unique_senders

        if not story["skipped"]:
            source_count_map[story["source_type"]] = source_count_map.get(
                story["source_type"], 0) + 1
        all_stories.append(story)

    for story in external_stories:
        ts = _parse_iso_ts(story["last_updated"])
        story["age_days"] = (now_t - ts) // 86400 if ts else None
        if not story["skipped"]:
            source_type = story["source_type"]
            source_count_map[source_type] = source_count_map.get(
                source_type, 0) + 1
        story["source_label"] = _source_label(story["source_type"])
        all_stories.append(story)

    source_counts = [
        {"source": key, "label": _source_label(key), "count": count}
        for key, count in sorted(source_count_map.items(), key=lambda item: (-item[1], item[0]))
    ]
    known_sources = {item["source"] for item in source_counts}
    if source != "all" and source not in known_sources:
        source = "all"

    clauses = parse_clauses(q) if q else []

    stories = all_stories
    if source != "all":
        stories = [
            story for story in stories if story["source_type"] == source]

    if q:
        wants_skipped = any(
            clause.get("op") == "is" and (clause.get("val") or "").lower() == "skipped"
            for clause in clauses
        )
        if not wants_skipped and state != "skipped":
            stories = [story for story in stories if not story["skipped"]]
        if clauses:
            stories = [
                story
                for story in stories
                if _match_story(
                    story,
                    clauses,
                    senders_by_story.get(story["id"], story.get("senders", [])),
                )
            ]
    elif state != "skipped":
        stories = [story for story in stories if not story["skipped"]]

    if state == "unread":
        stories = [story for story in stories if not story["is_read"]]
    elif state == "read":
        stories = [story for story in stories if story["is_read"]]
    elif state == "skipped":
        stories = [story for story in stories if story["skipped"]]

    def _score(story: dict) -> float:
        hours = ((story.get("age_days") or 0) * 24) + 1
        return story["mention_count"] * 3.0 + \
            story["new_info_count"] * 5.0 + 1.0 / hours

    if sort == "score":
        stories.sort(
            key=lambda story: (
                _score(story),
                story["last_updated"] or ""),
            reverse=True)
    elif sort == "mentions":
        stories.sort(
            key=lambda story: (
                story["mention_count"],
                story["last_updated"] or ""),
            reverse=True)
    elif sort == "evolved":
        stories.sort(
            key=lambda story: (
                story["new_info_count"],
                story["last_updated"] or ""),
            reverse=True)
    elif sort == "oldest":
        stories.sort(key=lambda story: story["last_updated"] or "")
    else:
        sort = "recent"
        stories.sort(
            key=lambda story: story["last_updated"] or "",
            reverse=True)

    total_unskipped = sum(source_count_map.values())

    return render_template(
        "digest_list.html",
        stories=stories,
        total=len(stories),
        total_all=total_unskipped,
        missing=False,
        source_counts=source_counts,
        q=q,
        sort=sort,
        state=state,
        source=source,
    )

@app.route("/api/digest/skip", methods=["POST"])
def api_digest_skip():
    data = request.get_json(silent=True) or {}
    story_id = (data.get("id") or "").strip()
    value = 1 if data.get("value", 1) else 0
    if not story_id:
        return jsonify({"error": "missing id"}), 400
    conn = _stories_conn(readonly=False)
    if conn is None:
        return jsonify({"error": "no stories db"}), 500
    cur = conn.execute(
        "UPDATE stories SET skipped=? WHERE id=?", (value, story_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        _mark_external_state(story_id, skipped=value)
    return jsonify({"id": story_id, "skipped": value})

@app.route("/api/digest/story/<story_id>")
def api_digest_story(story_id: str):
    conn = _stories_conn()
    if conn is None:
        return jsonify({"error": "no stories db"}), 404
    row = conn.execute(
        f"SELECT {STORY_DETAIL_COLUMNS} FROM stories WHERE id=?",
        (story_id,),
    ).fetchone()
    if not row:
        story = _get_external_story(conn, story_id)
        conn.close()
        if not story:
            return jsonify({"error": "not found"}), 404
        if not story.get("is_read"):
            _mark_external_state(story_id, is_read=1)
            story["is_read"] = 1
        mention = {
            "story_id": story_id,
            "title": story["title"],
            "summary": story["summary"],
            "sender": story.get("author") or story["source_type"],
            "gmail_url": story["primary_url"],
            "date": story["first_seen"] or story["last_updated"],
        }
        return jsonify({"story": story, "mentions": [mention], "timeline": []})
    story = dict(row)
    story["links"] = _parse_links_json(story.get("links_json") or "")
    mentions = [
        dict(mention)
        for mention in conn.execute(
            "SELECT * FROM mentions WHERE story_id=? ORDER BY date DESC",
            (story_id,),
        ).fetchall()
    ]
    timeline = [
        dict(entry)
        for entry in conn.execute(
            "SELECT * FROM timeline_entries WHERE story_id=? ORDER BY date DESC",
            (story_id,),
        ).fetchall()
    ]
    conn.close()

    if not story.get("is_read"):
        try:
            wconn = _stories_conn(readonly=False)
            if wconn is not None:
                wconn.execute(
                    "UPDATE stories SET is_read=1 WHERE id=?", (story_id,))
                wconn.commit()
                wconn.close()
                story["is_read"] = 1
        except Exception:
            pass

    return jsonify({"story": story,
                    "mentions": mentions,
                    "timeline": timeline})

@app.route("/api/digest/search-index")
def digest_search_index():
    conn = _stories_conn()
    if conn is None:
        return jsonify([])
    rows = conn.execute(
        "SELECT id, title, summary FROM stories ORDER BY last_updated DESC LIMIT 2000"
    ).fetchall()
    external_stories = _load_external_stories(conn)
    conn.close()
    items = [
        {
            "id": row["id"],
            "title": row["title"],
            "summary": (row["summary"] or "")[:140],
        }
        for row in rows
    ]
    items.extend(
        {
            "id": story["id"],
            "title": story["title"],
            "summary": (story["summary"] or "")[:140],
        }
        for story in external_stories
    )
    return jsonify(items)

@app.route("/digest/<story_id>")
def digest_story(story_id: str):
    conn = _stories_conn()
    if conn is None:
        return redirect("/digest")
    row = conn.execute(
        f"SELECT {STORY_DETAIL_COLUMNS} FROM stories WHERE id=?",
        (story_id,),
    ).fetchone()
    if row is None:
        story = _get_external_story(conn, story_id)
        conn.close()
        if story is None:
            return redirect("/digest")
        mention = {
            "story_id": story_id,
            "title": story["title"],
            "summary": story["summary"],
            "sender": story.get("author") or story["source_type"],
            "gmail_url": story["primary_url"],
            "date": story["first_seen"] or story["last_updated"],
        }
        return render_template(
            "digest_story.html",
            story=story,
            mentions=[mention],
            timeline=[],
        )
    story = dict(row)
    mentions = conn.execute(
        "SELECT * FROM mentions WHERE story_id=? ORDER BY date DESC",
        (story_id,),
    ).fetchall()
    timeline = conn.execute(
        "SELECT * FROM timeline_entries WHERE story_id=? ORDER BY date DESC",
        (story_id,),
    ).fetchall()
    conn.close()
    return render_template(
        "digest_story.html",
        story=story,
        mentions=[dict(mention) for mention in mentions],
        timeline=[dict(entry) for entry in timeline],
    )

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(
            os.environ.get(
                "PORT",
                "5000")))

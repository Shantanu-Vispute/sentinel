import json
import math
import os
import pathlib
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import humanize
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file
from config import STORIES_DB
from digest.media_cache import media_file_path
from digest.bestblogs_client import fetch_categories, fetch_resources, fetch_source_options
from digest.gmail_auth import get_gmail_service
from digest.gmail_fetcher import fetch_newsletter_by_id, message_id_from_gmail_url, normalize_email_text
from digest.sender_signal import sender_signal_for_story
from digest.story_quality import assess_story_quality

HERE = pathlib.Path(__file__).resolve().parent
STORIES_DB_PATH = pathlib.Path(STORIES_DB)
SCRIPTS_DIR = HERE / "scripts"
STATE_DIR = HERE / "state"
APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
SYNC_TARGETS = {
    "all": [
        ("email", "Email", SCRIPTS_DIR / "run-email.sh"),
        ("telegram", "Telegram", SCRIPTS_DIR / "run-telegram.sh"),
        ("social", "Social", SCRIPTS_DIR / "run-social.sh"),
        ("youtube", "YouTube", SCRIPTS_DIR / "run-youtube.sh"),
        ("bestblogs", "BestBlogs", SCRIPTS_DIR / "run-bestblogs.sh"),
    ],
    "email": [("email", "Email", SCRIPTS_DIR / "run-email.sh")],
    "telegram": [("telegram", "Telegram", SCRIPTS_DIR / "run-telegram.sh")],
    "twitter": [("social", "Social", SCRIPTS_DIR / "run-social.sh")],
    "linkedin": [("social", "Social", SCRIPTS_DIR / "run-social.sh")],
    "youtube": [("youtube", "YouTube", SCRIPTS_DIR / "run-youtube.sh")],
    "bestblogs": [("bestblogs", "BestBlogs", SCRIPTS_DIR / "run-bestblogs.sh")],
}
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


@app.get("/media/<path:rel_path>")
def media(rel_path: str):
    path = media_file_path(rel_path)
    if path is None or not path.exists() or not path.is_file():
        abort(404)
    return send_file(path)

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
    dt = _parse_iso_dt(s)
    if dt is None:
        return 0
    return int(dt.timestamp())

def _parsed_ts(s: str) -> float:
    dt = _parse_iso_dt(s or "")
    return dt.timestamp() if dt is not None else 0.0

def _mention_sort_key(mention: dict) -> tuple[float, float, int]:
    return (
        _parsed_ts(mention.get("date") or ""),
        _parsed_ts(mention.get("created_at") or ""),
        int(mention.get("id") or 0),
    )

def _timeline_sort_key(entry: dict) -> int:
    # timeline_entries.date is the source email's own header, which is
    # frequently stale/out-of-order across senders (confirmed against real
    # data: a single story's 42 entries were not chronological by date).
    # id (an AUTOINCREMENT primary key) is true insertion order.
    return int(entry.get("id") or 0)

def _parse_iso_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = parsedate_to_datetime(s)
        except Exception:
            return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)

    now_utc = datetime.now(timezone.utc)
    candidates = [
        dt.replace(tzinfo=timezone.utc),
        dt.replace(tzinfo=APP_TIMEZONE).astimezone(timezone.utc),
    ]
    nonfuture = [
        candidate for candidate in candidates
        if candidate <= now_utc + timedelta(minutes=5)
    ]
    pool = nonfuture or candidates
    return min(
        pool,
        key=lambda candidate: abs((now_utc - candidate).total_seconds()),
    )

def _relative_time_label(s: str) -> str:
    dt = _parse_iso_dt(s)
    return _relative_time_label_dt(dt)

def _relative_time_label_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    try:
        return humanize.naturaltime(datetime.now(timezone.utc) - dt)
    except Exception:
        return ""

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
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(APP_TIMEZONE).isoformat()
    except Exception:
        return ""

def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None

def _mentions_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(mentions)")}

def _ensure_mentions_raw_columns(conn: sqlite3.Connection) -> set[str]:
    cols = _mentions_columns(conn)
    if "raw_title" not in cols:
        conn.execute("ALTER TABLE mentions ADD COLUMN raw_title TEXT")
    if "raw_body" not in cols:
        conn.execute("ALTER TABLE mentions ADD COLUMN raw_body TEXT")
    if cols != _mentions_columns(conn):
        conn.commit()
    return _mentions_columns(conn)

def _renderable_mention(mention: dict) -> dict:
    rendered = dict(mention)
    if (rendered.get("source_type") or "email") == "email":
        title = normalize_email_text(rendered.get("raw_title") or rendered.get("title") or "")
        summary = normalize_email_text(rendered.get("raw_body") or rendered.get("summary") or "")
        if title and summary.startswith(title) and len(summary) > len(title):
            next_char = summary[len(title)]
            if next_char.isalnum():
                summary = title + "\n\n" + summary[len(title):].lstrip()
        rendered["title"] = title
        rendered["summary"] = summary
    return rendered

_HYDRATION_INFLIGHT: set[str] = set()
_HYDRATION_LOCK = threading.Lock()
_EXPLORE_CACHE: dict[tuple, tuple[float, dict]] = {}
_EXPLORE_CACHE_LOCK = threading.Lock()
_EXPLORE_CACHE_TTL = 120


def _hydrate_email_mentions(conn: sqlite3.Connection, mentions: list[dict]) -> list[dict]:
    cols = _ensure_mentions_raw_columns(conn)
    can_store = "raw_title" in cols and "raw_body" in cols and "id" in cols
    if can_store:
        pending = [
            {"id": mention["id"], "gmail_url": mention["gmail_url"]}
            for mention in mentions
            if (mention.get("source_type") or "email") == "email"
            and mention.get("gmail_url")
            and mention.get("id")
            and (not mention.get("raw_title") or not mention.get("raw_body"))
        ]
        if pending:
            _spawn_gmail_hydration(pending)
    return [_renderable_mention(mention) for mention in mentions]


def _spawn_gmail_hydration(pending: list[dict]) -> None:
    with _HYDRATION_LOCK:
        fresh = [
            item for item in pending
            if item["id"] not in _HYDRATION_INFLIGHT
        ]
        if not fresh:
            return
        for item in fresh:
            _HYDRATION_INFLIGHT.add(item["id"])
    threading.Thread(
        target=_hydrate_gmail_in_background,
        args=(fresh,),
        daemon=True,
    ).start()


def _hydrate_gmail_in_background(pending: list[dict]) -> None:
    try:
        try:
            service = get_gmail_service()
        except Exception:
            return
        conn = _stories_conn(readonly=False)
        if conn is None:
            return
        try:
            fetched: dict = {}
            dirty = False
            for item in pending:
                message_id = message_id_from_gmail_url(item.get("gmail_url") or "")
                if not message_id:
                    continue
                if message_id not in fetched:
                    try:
                        fetched[message_id] = fetch_newsletter_by_id(service, message_id)
                    except Exception:
                        fetched[message_id] = None
                newsletter = fetched[message_id]
                if newsletter is None:
                    continue
                try:
                    conn.execute(
                        "UPDATE mentions SET raw_title=?, raw_body=? WHERE id=?",
                        (newsletter.subject, newsletter.body, item["id"]),
                    )
                    dirty = True
                except Exception:
                    pass
            if dirty:
                conn.commit()
        finally:
            conn.close()
    finally:
        with _HYDRATION_LOCK:
            for item in pending:
                _HYDRATION_INFLIGHT.discard(item["id"])

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
    saved_ts = row["scraped_at"] or row["last_seen_at"] or row["created_ts"] or created_ts
    source_rank = row["source_rank"] if "source_rank" in row.keys() else None
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
        "last_updated": _iso_from_ts(saved_ts),
        "age_days": None,
        "skipped": bool(row["skipped"] or 0),
        "source_type": source,
        "source_label": _source_label(source),
        "primary_image_url": row["cover"] or "",
        "links": links,
        "senders": [author] if author else [source],
        "author": author,
        "source_rank": int(source_rank or 0),
        "sort_ts": saved_ts,
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

_EXPLORE_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "using",
    "your", "you", "are", "was", "were", "about", "after", "before", "over",
    "under", "new", "now", "how", "why", "what", "when", "where", "more",
    "less", "than", "have", "has", "had", "not", "but", "can", "lets",
}
_EXPLORE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]{2,}", re.I)
_EXPLORE_QUESTION_RE = re.compile(
    r"\b(question|help wanted|how do i|how to avoid|anyone know|issue|"
    r"broken|permission pop-?up|cannot open)\b",
    re.I,
)
_EXPLORE_EVENT_RE = re.compile(
    r"\b(waitlist|livestream|webinar|registration|tickets|meetup|conference|"
    r"join us|today's event|event)\b",
    re.I,
)
_EXPLORE_GUIDE_RE = re.compile(
    r"\b(guide|tutorial|best practices|lessons from|case study|workflow|"
    r"principles|how to|tips|playbook)\b",
    re.I,
)
_EXPLORE_METRIC_RE = re.compile(
    r"\b(downloads?|followers?|views?|stars?|users?|waitlist|hit[s]?|surge|"
    r"past \d|[0-9]+[kmb]?/week)\b",
    re.I,
)
_EXPLORE_MATERIAL_RE = re.compile(
    r"\b(releases?|launch(?:es|ed)?|announc(?:es|ed)?|unveils?|upgrade|"
    r"model|gpt-|claude|gemini|llama|qwen|deepseek|mistral|cosmos|"
    r"openai|anthropic|deepmind|nvidia|security|cyber|policy|governance|"
    r"frontier ai|benchmark|dataset|paper|technical report|funding|ipo)\b",
    re.I,
)
_EXPLORE_POLICY_SECURITY_RE = re.compile(
    r"\b(policy|governance|security|cyber|vulnerab|safety|frontier ai|"
    r"executive order|regulat|threat)\b",
    re.I,
)
_EXPLORE_SOFT_CONTENT_RE = re.compile(
    r"\b(podcast|interview|conversation|crossover|special|course|webinar|"
    r"roundup|recap|lessons|best practices|i spent|what lies beneath)\b",
    re.I,
)
_EXPLORE_STRONG_SOURCE_RE = re.compile(
    r"\b(openai|anthropic|google deepmind|deepmind|google gemini|meta ai|"
    r"nvidia|minimax|mistral|hugging face|simon willison|greg brockman|"
    r"latent\.space|ai engineer)\b",
    re.I,
)
_EXPLORE_NOISY_SOURCE_RE = re.compile(
    r"\b(langchain|claudedevs|god of prompt|peter steinberger)\b",
    re.I,
)


def _explore_cache_get(key: tuple) -> dict | None:
    with _EXPLORE_CACHE_LOCK:
        hit = _EXPLORE_CACHE.get(key)
        if not hit:
            return None
        created, data = hit
        if time.time() - created > _EXPLORE_CACHE_TTL:
            _EXPLORE_CACHE.pop(key, None)
            return None
        return data


def _explore_cache_set(key: tuple, data: dict) -> None:
    with _EXPLORE_CACHE_LOCK:
        if len(_EXPLORE_CACHE) > 64:
            oldest = sorted(_EXPLORE_CACHE.items(), key=lambda item: item[1][0])[:16]
            for old_key, _ in oldest:
                _EXPLORE_CACHE.pop(old_key, None)
        _EXPLORE_CACHE[key] = (time.time(), data)


def _explore_item_id(item: dict) -> str:
    raw_id = str(item.get("id") or item.get("readUrl") or item.get("url") or "")
    if not raw_id:
        return ""
    return raw_id if raw_id.startswith("BB_") else f"BB_{raw_id}"


def _explore_state_map(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    conn = _stories_conn()
    if conn is None or not _has_table(conn, "external_item_state"):
        if conn:
            conn.close()
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, is_read, skipped FROM external_item_state WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    conn.close()
    return {
        row["id"]: {"is_read": bool(row["is_read"]), "skipped": bool(row["skipped"])}
        for row in rows
    }


def _explore_parse_dt(item: dict) -> datetime | None:
    raw = item.get("publishDateTimeStr") or item.get("publishDateStr") or ""
    if not raw or raw == "Today":
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        pass
    ts = item.get("publishTimeStamp")
    try:
        ts_int = int(ts)
        if ts_int > 10_000_000_000:
            ts_int //= 1000
        return datetime.fromtimestamp(ts_int, tz=timezone.utc)
    except Exception:
        return None


def _explore_text(item: dict) -> str:
    return " ".join(
        str(part or "")
        for part in [
            item.get("title"),
            item.get("originalTitle"),
            item.get("oneSentenceSummary"),
            item.get("summary"),
            item.get("sourceName"),
            item.get("domain"),
            " ".join(item.get("tags") or []),
        ]
    )


def _explore_tokens(item: dict) -> set[str]:
    return {
        token.lower()
        for token in _EXPLORE_TOKEN_RE.findall(_explore_text(item))
        if token.lower() not in _EXPLORE_STOPWORDS
    }


def _explore_recency(age_hours: float) -> tuple[float, float]:
    if age_hours <= 3:
        return 7.0, 1.2
    if age_hours <= 12:
        return 5.5, 1.0
    if age_hours <= 24:
        return 3.0, 0.75
    if age_hours <= 36:
        return 1.5, 0.45
    if age_hours <= 72:
        return 0.0, 0.18
    return -2.0, 0.05


def _score_explore_item(item: dict, now: datetime) -> tuple[float, list[str]]:
    text = _explore_text(item)
    title_text = " ".join(
        str(part or "")
        for part in [
            item.get("title"),
            item.get("originalTitle"),
            item.get("oneSentenceSummary"),
        ]
    )
    source_text = " ".join(
        str(part or "")
        for part in [
            item.get("sourceName"),
            item.get("domain"),
            item.get("author"),
        ]
    )
    labels = []
    published = _explore_parse_dt(item)
    age_hours = (
        max((now - published).total_seconds() / 3600.0, 0.0)
        if published
        else 9999.0
    )
    recency_bonus, strength_weight = _explore_recency(age_hours)
    bestblogs_score = max(min(int(item.get("score") or 0), 100), 0)
    score = recency_bonus + ((bestblogs_score - 75) / 5.0) * strength_weight

    resource_type = (item.get("resourceType") or "").upper()
    if resource_type == "ARTICLE":
        score += 0.6
    elif resource_type == "VIDEO":
        score -= 0.6
    elif resource_type == "PODCAST":
        score -= 1.0

    read_count = max(int(item.get("readCount") or 0), 0)
    score += min(math.log1p(read_count) * 0.25, 1.4) * strength_weight

    if _EXPLORE_STRONG_SOURCE_RE.search(source_text):
        score += 2.2 * strength_weight
        labels.append("strong_source")
    elif _EXPLORE_NOISY_SOURCE_RE.search(source_text):
        score -= 1.2
        labels.append("repetitive_source")

    has_material = bool(_EXPLORE_MATERIAL_RE.search(text))
    if has_material:
        score += 1.4 * strength_weight
        labels.append("material_ai")
    if _EXPLORE_POLICY_SECURITY_RE.search(text):
        score += 1.2 * strength_weight
        labels.append("policy_security")

    if _EXPLORE_QUESTION_RE.search(text):
        score -= 4.0
        labels.append("question_or_issue")
    if _EXPLORE_EVENT_RE.search(text) and not _EXPLORE_POLICY_SECURITY_RE.search(text):
        score -= 3.0
        labels.append("event_promo")
    if _EXPLORE_SOFT_CONTENT_RE.search(title_text):
        score -= 2.2
        labels.append("soft_content")
    if _EXPLORE_METRIC_RE.search(text) and "benchmark" not in text.lower():
        score -= 2.2
        labels.append("metric_milestone")
    if _EXPLORE_GUIDE_RE.search(text) and not has_material:
        score -= 2.0
        labels.append("guide_or_tutorial")

    if item.get("language") not in ("en_US", "en"):
        score -= 0.8
        labels.append("non_english")

    if item.get("isRead"):
        score -= 8.0
        labels.append("read")

    item["localScore"] = round(score, 3)
    item["localSignals"] = labels[:6]
    return score, labels


def _explore_is_duplicate(a: dict, b: dict) -> bool:
    if a.get("url") and a.get("url") == b.get("url"):
        return True
    if a.get("readUrl") and a.get("readUrl") == b.get("readUrl"):
        return True
    dt_a = _explore_parse_dt(a)
    dt_b = _explore_parse_dt(b)
    if dt_a and dt_b and abs((dt_a - dt_b).total_seconds()) > 10 * 3600:
        return False
    tok_a = a.get("_tokens") or _explore_tokens(a)
    tok_b = b.get("_tokens") or _explore_tokens(b)
    if not tok_a or not tok_b:
        return False
    shared = tok_a & tok_b
    jaccard = len(shared) / max(len(tok_a | tok_b), 1)
    entities = {
        "openai", "anthropic", "claude", "codex", "langchain", "langsmith",
        "openclaw", "gemini", "rosalind", "gpt-rosalind", "nvidia", "cosmos",
        "minimax", "mistral", "qwen", "deepseek",
    }
    return jaccard >= 0.32 or (len(shared) >= 4 and bool(shared & entities))


def _rerank_explore_items(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    scored = []
    for item in items:
        item["_tokens"] = _explore_tokens(item)
        score, _labels = _score_explore_item(item, now)
        scored.append((score, item))
    scored.sort(
        key=lambda pair: (
            pair[0],
            int(pair[1].get("score") or 0),
            pair[1].get("publishDateTimeStr") or "",
        ),
        reverse=True,
    )

    deduped: list[dict] = []
    for _score, item in scored:
        duplicate_of = None
        for kept in deduped[:120]:
            if _explore_is_duplicate(item, kept):
                duplicate_of = kept
                break
        if duplicate_of is not None:
            duplicate_of["localDuplicateCount"] = int(
                duplicate_of.get("localDuplicateCount") or 0
            ) + 1
            continue
        deduped.append(item)

    source_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    remaining = list(deduped)
    balanced = []
    while remaining:
        best_idx = 0
        best_score = -10_000.0
        for idx, item in enumerate(remaining[:160]):
            source = item.get("sourceName") or ""
            domain = item.get("domain") or ""
            adjusted = float(item.get("localScore") or 0)
            adjusted -= source_counts[source] * 1.8
            adjusted -= domain_counts[domain] * 0.8
            if adjusted > best_score:
                best_idx = idx
                best_score = adjusted
        item = remaining.pop(best_idx)
        source_counts[item.get("sourceName") or ""] += 1
        domain_counts[item.get("domain") or ""] += 1
        item.pop("_tokens", None)
        balanced.append(item)
    return balanced


def _fetch_explore_candidates(params: dict) -> dict:
    raw_params = dict(params)
    raw_params["pageSize"] = 100
    raw_params["page"] = 1
    cache_key = tuple(sorted((str(k), str(v)) for k, v in raw_params.items()))
    cached = _explore_cache_get(cache_key)
    if cached is not None:
        return cached

    first = fetch_resources(raw_params)
    payload = first.get("data") or {}
    items = list(payload.get("dataList") or [])
    page_count = int(payload.get("pageCount") or 1)
    pages_to_fetch = min(page_count, 2)
    for raw_page in range(2, pages_to_fetch + 1):
        next_params = dict(raw_params)
        next_params["page"] = raw_page
        try:
            next_data = fetch_resources(next_params)
        except Exception:
            break
        items.extend((next_data.get("data") or {}).get("dataList") or [])

    payload = dict(payload)
    payload["dataList"] = items
    payload["rawCandidateCount"] = len(items)
    first = dict(first)
    first["data"] = payload
    _explore_cache_set(cache_key, first)
    return first


def _postprocess_explore_resources(params: dict) -> dict:
    sort = (params.get("sort") or params.get("sortType") or "time_desc").strip()
    local_rank = str(params.get("localRank") or params.get("local_rank") or "1") != "0"
    if not local_rank or sort not in {"time_desc", "time"}:
        data = fetch_resources(params)
        payload = data.get("data") or {}
        items = payload.get("dataList") or []
        ids = [_explore_item_id(item) for item in items if _explore_item_id(item)]
        state = _explore_state_map(ids)
        for item in items:
            item_id = _explore_item_id(item)
            item["localItemId"] = item_id
            item_state = state.get(item_id, {})
            item["isRead"] = bool(item_state.get("is_read", False))
            item["skipped"] = bool(item_state.get("skipped", False))
        payload = dict(payload)
        payload["dataList"] = [item for item in items if not item.get("skipped")]
        result = dict(data)
        result["data"] = payload
        return result
    else:
        data = _fetch_explore_candidates(params)
        payload = data.get("data") or {}
        items = list(payload.get("dataList") or [])
    raw_total = payload.get("totalCount")

    ids = [_explore_item_id(item) for item in items if _explore_item_id(item)]
    state = _explore_state_map(ids)
    for item in items:
        item_id = _explore_item_id(item)
        item["localItemId"] = item_id
        item_state = state.get(item_id, {})
        item["isRead"] = bool(item_state.get("is_read", False))
        item["skipped"] = bool(item_state.get("skipped", False))
    items = [item for item in items if not item.get("skipped")]

    if local_rank and sort in {"time_desc", "time"}:
        items = _rerank_explore_items(items)

    page = max(int(params.get("page") or 1), 1)
    page_size = min(max(int(params.get("pageSize") or params.get("page_size") or 10), 1), 100)
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = items[start:end]
    page_count = max(math.ceil(total / page_size), 1)

    payload = dict(payload)
    payload["dataList"] = page_items
    payload["currentPage"] = page
    payload["pageCount"] = page_count
    payload["totalCount"] = total
    payload["localRanked"] = bool(local_rank and sort in {"time_desc", "time"})
    payload["rawTotalCount"] = raw_total
    result = dict(data)
    result["data"] = payload
    return result

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


def _sync_button_label(source: str) -> str:
    if not source or source == "all":
        return "Sync all"
    return f"Sync {_source_label(source)}"


def _list_running_commands() -> list[str]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _script_running(script_path: pathlib.Path) -> bool:
    target = str(script_path)
    for line in _list_running_commands():
        if target in line and "python" not in line[:12]:
            return True
    return False


def _launch_sync_script(script_path: pathlib.Path) -> tuple[bool, str]:
    if not script_path.exists():
        return False, "missing"
    if _script_running(script_path):
        return False, "already_running"
    try:
        subprocess.Popen(
            [str(script_path)],
            cwd=str(HERE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, "started"
    except Exception as exc:
        return False, str(exc)


def _read_sync_status(job: str, label: str) -> dict:
    path = STATE_DIR / f"sync_status_{job}.json"
    data = {
        "job": job,
        "label": label,
        "status": "idle",
        "ok": False,
        "message": "",
        "started_at": "",
        "finished_at": "",
        "updated_at": "",
        "log_path": "",
    }
    if not path.exists():
        return data
    try:
        payload = json.loads(path.read_text())
    except Exception:
        data["status"] = "unknown"
        data["message"] = "Status file unreadable"
        return data
    data.update({
        "status": payload.get("status", "idle"),
        "ok": bool(payload.get("ok", False)),
        "message": payload.get("message", ""),
        "started_at": payload.get("started_at", ""),
        "finished_at": payload.get("finished_at", ""),
        "updated_at": payload.get("updated_at", ""),
        "log_path": payload.get("log_path", ""),
    })
    return data


def _all_sync_statuses() -> list[dict]:
    seen = set()
    statuses = []
    for targets in SYNC_TARGETS.values():
        for key, label, _ in targets:
            if key in seen:
                continue
            seen.add(key)
            statuses.append(_read_sync_status(key, label))
    order = {"email": 0, "telegram": 1, "social": 2, "youtube": 3}
    statuses.sort(key=lambda item: order.get(item["job"], 99))
    return statuses

@app.route("/")
def index():
    return redirect("/home")

@app.route("/explore")
def explore():
    return render_template(
        "explore.html",
        type=(request.args.get("type") or "ALL").upper(),
        sort=request.args.get("sort") or "time_desc",
        category=request.args.get("category") or "ai",
        time_filter=request.args.get("timeFilter") or request.args.get("time_filter") or "all",
        language=request.args.get("language") or "en_US",
        page=max(int(request.args.get("page") or 1), 1),
    )

@app.route("/api/explore/resources")
def api_explore_resources():
    try:
        params = dict(request.args)
        params.setdefault("language", "en_US")
        return jsonify(_postprocess_explore_resources(params))
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 502

@app.route("/api/explore/categories")
def api_explore_categories():
    try:
        return jsonify(fetch_categories())
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 502

@app.route("/api/explore/sources")
def api_explore_sources():
    try:
        return jsonify(fetch_source_options(
            resource_type=request.args.get("type") or request.args.get("resourceType") or "ALL",
            page=int(request.args.get("page") or 1),
            page_size=int(request.args.get("pageSize") or 100),
        ))
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 502

@app.route("/home")
def home_feed():
    return _render_digest_feed(nav_active="home", feed_path="/home")

@app.route("/digest")
def digest_list():
    return _render_digest_feed(
        nav_active="following", feed_path="/digest",
        default_excluded_sources={"bestblogs"},
    )

def _render_digest_feed(
        nav_active: str,
        feed_path: str,
        default_excluded_sources: frozenset = frozenset()):
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
            sync_label=_sync_button_label("all"),
            sync_statuses=_all_sync_statuses(),
            nav_active=nav_active,
            feed_path=feed_path,
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
    mention_rows = conn.execute(
        """SELECT story_id, sender, date, COALESCE(source_type, 'email') AS source_type
             FROM mentions"""
    ).fetchall()
    # Latest timeline entry per story, by insertion order (id) rather than the
    # `date` column — `date` is the source email's own header, which is
    # frequently stale/out-of-order across senders, so it's unreliable for
    # "what actually changed most recently."
    latest_update_rows = conn.execute(
        """SELECT t.story_id, t.what_changed, t.trigger_sender
             FROM timeline_entries t
             INNER JOIN (
                 SELECT story_id, MAX(id) AS max_id
                   FROM timeline_entries
                  GROUP BY story_id
             ) latest ON t.story_id = latest.story_id AND t.id = latest.max_id"""
    ).fetchall()
    external_stories = _load_external_stories(conn)
    conn.close()

    latest_update_by_story = {
        row["story_id"]: {
            "what_changed": row["what_changed"],
            "trigger_sender": row["trigger_sender"] or "",
        }
        for row in latest_update_rows
    }

    senders_by_story: dict[str, list[str]] = {}
    mention_sources_by_story: dict[str, list[str]] = {}
    latest_mention_dt_by_story: dict[str, datetime] = {}
    for row in mention_rows:
        senders_by_story.setdefault(row["story_id"], []).append(row["sender"] or "")
        mention_sources_by_story.setdefault(
            row["story_id"], []
        ).append((row["source_type"] or "email").strip().lower())
        mention_dt = _parse_iso_dt(row["date"] or "")
        if mention_dt is None:
            continue
        current_dt = latest_mention_dt_by_story.get(row["story_id"])
        if current_dt is None or mention_dt > current_dt:
            latest_mention_dt_by_story[row["story_id"]] = mention_dt

    now_t = int(time.time())
    q = (request.args.get("q") or "").strip()

    sort = (request.args.get("sort") or "recent").strip().lower()
    state = (request.args.get("state") or "").strip().lower()
    requested_source_param = (request.args.get("source") or "").strip().lower()
    source = requested_source_param or "all"

    all_stories = []
    source_count_map: dict[str, int] = {}

    for row in rows:
        ts = _parse_iso_ts(row["last_updated"])
        latest_mention_dt = latest_mention_dt_by_story.get(row["id"])
        source_type = (row["source_type"] or "email").strip().lower()
        if source_type == "email" and latest_mention_dt is not None:
            ts = int(latest_mention_dt.timestamp())
            display_time = latest_mention_dt.astimezone(APP_TIMEZONE).isoformat()
            display_relative = _relative_time_label_dt(latest_mention_dt)
        else:
            display_time = row["last_updated"] or ""
            display_relative = _relative_time_label(display_time)
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
            "last_updated_display": display_time,
            "last_updated_relative": display_relative,
            "first_seen_relative": _relative_time_label(row["first_seen"] or ""),
            "age_days": age_days,
            "skipped": bool(row["skipped"]),
            "source_type": source_type,
            "source_label": _source_label(source_type),
            "primary_image_url": row["primary_image_url"] or "",
            "links": _parse_links_json(row["links_json"]),
            "senders": [],
            "source_rank": 0,
            "sort_ts": ts,
            "latest_update": latest_update_by_story.get(row["id"], {}).get("what_changed", ""),
        }
        seen_senders = set()
        unique_senders = []
        for sender in senders_by_story.get(row["id"], []):
            key = (sender or "").strip()
            if key and key.lower() not in seen_senders:
                seen_senders.add(key.lower())
                unique_senders.append(key)
        story["senders"] = unique_senders
        story["distinct_sender_count"] = len(unique_senders)
        sender_signal = sender_signal_for_story(unique_senders, source_type=source_type)
        quality_sender = max(
            unique_senders,
            key=lambda sender: sender_signal_for_story([sender], source_type=source_type).score,
            default="",
        )

        quality = assess_story_quality(
            story["title"],
            story["summary"],
            source_type=source_type,
            sender=quality_sender,
        )
        story["quality_score"] = quality.score
        story["quality_reason"] = quality.reason
        story["sender_signal_score"] = sender_signal.score
        story["sender_signal_label"] = sender_signal.label
        story["trusted_early_sender_count"] = sender_signal.trusted_early_count
        story["noisy_fast_sender_count"] = sender_signal.noisy_fast_count
        story["dev_lane_sender_count"] = sender_signal.dev_lane_count
        raw_source_types = mention_sources_by_story.get(row["id"], [])
        if raw_source_types:
            story["distinct_source_types"] = sorted(set(raw_source_types))
        else:
            story["distinct_source_types"] = [story["source_type"]]

        if not story["skipped"]:
            source_count_map[story["source_type"]] = source_count_map.get(
                story["source_type"], 0) + 1
        all_stories.append(story)

    for story in external_stories:
        ts = _parse_iso_ts(story["last_updated"])
        story["age_days"] = (now_t - ts) // 86400 if ts else None
        story["last_updated_display"] = story.get("last_updated") or ""
        story["last_updated_relative"] = _relative_time_label(story.get("last_updated") or "")
        story["first_seen_relative"] = _relative_time_label(story.get("first_seen") or "")
        if not story["skipped"]:
            source_type = story["source_type"]
            source_count_map[source_type] = source_count_map.get(
                source_type, 0) + 1
        story["source_label"] = _source_label(story["source_type"])
        story["distinct_sender_count"] = len(story.get("senders", []))
        story["distinct_source_types"] = [story["source_type"]]
        story["quality_score"] = 0.0
        story["quality_reason"] = ""
        story["sender_signal_score"] = 0.0
        story["sender_signal_label"] = ""
        story["trusted_early_sender_count"] = 0
        story["noisy_fast_sender_count"] = 0
        story["dev_lane_sender_count"] = 0
        all_stories.append(story)

    # Sidebar facets omit default_excluded_sources (e.g. Following hides the
    # BestBlogs facet), but `known_sources` for query-param validation stays
    # the full set — an explicit ?source=bestblogs must still work on either
    # route, only the *implicit* default view differs between Home/Following.
    nav_count_map = {
        k: v for k, v in source_count_map.items() if k not in default_excluded_sources
    }
    source_counts = [
        {"source": key, "label": _source_label(key), "count": count}
        for key, count in sorted(nav_count_map.items(), key=lambda item: (-item[1], item[0]))
    ]
    known_sources = set(source_count_map.keys())
    if source != "all" and source not in known_sources:
        source = "all"

    clauses = parse_clauses(q) if q else []

    stories = all_stories
    if source != "all":
        stories = [
            story for story in stories if story["source_type"] == source]
    elif not requested_source_param and default_excluded_sources:
        stories = [
            story for story in stories
            if story["source_type"] not in default_excluded_sources]

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

    def _sort_rank(story: dict) -> int:
        rank = int(story.get("source_rank") or 0)
        return -rank if rank else 0

    if sort == "mentions":
        stories.sort(
            key=lambda story: (
                story["mention_count"],
                story.get("sort_ts") or 0,
                _sort_rank(story),
                story["last_updated"] or ""),
            reverse=True)
    elif sort == "evolved":
        stories.sort(
            key=lambda story: (
                story["new_info_count"],
                story.get("sort_ts") or 0,
                _sort_rank(story),
                story["last_updated"] or ""),
            reverse=True)
    elif sort == "oldest":
        stories.sort(key=lambda story: (story.get("sort_ts") or 0, -_sort_rank(story), story["last_updated"] or ""))
    elif sort == "recent":
        stories.sort(
            key=lambda story: (
                story.get("sort_ts") or 0,
                _sort_rank(story),
                story["last_updated"] or "",
            ),
            reverse=True)
    else:
        sort = "recent"
        stories.sort(
            key=lambda story: (
                story.get("sort_ts") or 0,
                _sort_rank(story),
                story["last_updated"] or "",
            ),
            reverse=True)

    total_unskipped = sum(nav_count_map.values())

    try:
        per_page = int(request.args.get("per_page") or 50)
    except ValueError:
        per_page = 50
    per_page = max(10, min(per_page, 200))
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    page = max(page, 1)

    total = len(stories)
    start = (page - 1) * per_page
    end = start + per_page
    page_stories = stories[start:end]
    has_more = end < total

    if (request.args.get("partial") or "").strip() == "1":
        return render_template(
            "_digest_cards_page.html",
            stories=page_stories,
            has_more=has_more,
            page=page,
            per_page=per_page,
            total=total,
        )

    return render_template(
        "digest_list.html",
        stories=page_stories,
        total=total,
        total_all=total_unskipped,
        missing=False,
        source_counts=source_counts,
        q=q,
        sort=sort,
        state=state,
        source=source,
        sync_label=_sync_button_label(source),
        sync_statuses=_all_sync_statuses(),
        page=page,
        per_page=per_page,
        has_more=has_more,
        nav_active=nav_active,
        feed_path=feed_path,
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


@app.route("/api/digest/read", methods=["POST"])
def api_digest_read():
    data = request.get_json(silent=True) or {}
    story_id = (data.get("id") or "").strip()
    value = 1 if data.get("value", 1) else 0
    if not story_id:
        return jsonify({"error": "missing id"}), 400
    conn = _stories_conn(readonly=False)
    if conn is None:
        return jsonify({"error": "no stories db"}), 500
    cur = conn.execute(
        "UPDATE stories SET is_read=? WHERE id=?", (value, story_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        _mark_external_state(story_id, is_read=value)
    return jsonify({"id": story_id, "is_read": value})


@app.route("/api/digest/sync", methods=["POST"])
def api_digest_sync():
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "all").strip().lower()
    targets = SYNC_TARGETS.get(source)
    if not targets:
        return jsonify({"error": "unsupported source"}), 400

    started = []
    skipped = []
    failed = []
    seen = set()

    for key, label, script_path in targets:
        if key in seen:
            continue
        seen.add(key)
        ok, status = _launch_sync_script(script_path)
        item = {"key": key, "label": label, "status": status}
        if ok:
            started.append(item)
        elif status == "already_running":
            skipped.append(item)
        else:
            failed.append(item)

    return jsonify({
        "source": source,
        "started": started,
        "skipped": skipped,
        "failed": failed,
    })


@app.route("/api/digest/sync-status")
def api_digest_sync_status():
    return jsonify({"jobs": _all_sync_statuses()})

@app.route("/api/digest/story/<story_id>")
def api_digest_story(story_id: str):
    conn = _stories_conn(readonly=False)
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
            "date_relative": _relative_time_label(story["first_seen"] or story["last_updated"] or ""),
        }
        story["last_updated_relative"] = _relative_time_label(story.get("last_updated") or "")
        story["first_seen_relative"] = _relative_time_label(story.get("first_seen") or "")
        return jsonify({"story": story, "mentions": [mention], "timeline": []})
    story = dict(row)
    story["links"] = _parse_links_json(story.get("links_json") or "")
    story["last_updated_relative"] = _relative_time_label(story.get("last_updated") or "")
    story["first_seen_relative"] = _relative_time_label(story.get("first_seen") or "")
    mentions = [
        dict(mention)
        for mention in conn.execute(
            "SELECT * FROM mentions WHERE story_id=?",
            (story_id,),
        ).fetchall()
    ]
    mentions.sort(key=_mention_sort_key, reverse=True)
    mentions = _hydrate_email_mentions(conn, mentions)
    for mention in mentions:
        mention["date_relative"] = _relative_time_label(mention.get("date") or "")
    timeline = [
        dict(entry)
        for entry in conn.execute(
            "SELECT * FROM timeline_entries WHERE story_id=?",
            (story_id,),
        ).fetchall()
    ]
    timeline.sort(key=_timeline_sort_key, reverse=True)
    for entry in timeline:
        entry["date_relative"] = _relative_time_label(entry.get("date") or "")
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
    conn = _stories_conn(readonly=False)
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
            "date_relative": _relative_time_label(story["first_seen"] or story["last_updated"] or ""),
        }
        story["last_updated_relative"] = _relative_time_label(story.get("last_updated") or "")
        story["first_seen_relative"] = _relative_time_label(story.get("first_seen") or "")
        return render_template(
            "digest_story.html",
            story=story,
            mentions=[mention],
            timeline=[],
        )
    story = dict(row)
    story["last_updated_relative"] = _relative_time_label(story.get("last_updated") or "")
    story["first_seen_relative"] = _relative_time_label(story.get("first_seen") or "")
    mentions = conn.execute(
        "SELECT * FROM mentions WHERE story_id=?",
        (story_id,),
    ).fetchall()
    mention_dicts = [dict(mention) for mention in mentions]
    mention_dicts.sort(key=_mention_sort_key, reverse=True)
    mention_dicts = _hydrate_email_mentions(conn, mention_dicts)
    for mention in mention_dicts:
        mention["date_relative"] = _relative_time_label(mention.get("date") or "")
    timeline = conn.execute(
        "SELECT * FROM timeline_entries WHERE story_id=?",
        (story_id,),
    ).fetchall()
    timeline_entries = [dict(entry) for entry in timeline]
    timeline_entries.sort(key=_timeline_sort_key, reverse=True)
    conn.close()
    return render_template(
        "digest_story.html",
        story=story,
        mentions=mention_dicts,
        timeline=[
            {**entry, "date_relative": _relative_time_label(entry.get("date") or "")}
            for entry in timeline_entries
        ],
    )

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(
            os.environ.get(
                "PORT",
                "5000")))

import argparse
import json
import pathlib
import sqlite3
import subprocess
import sys
import time
from shutil import which
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent.parent
STATE_DIR = HERE / "state"
PROFILE_DIR = STATE_DIR / "browser_profile"
COOKIES_FILE = STATE_DIR / "youtube_cookies.txt"
DB_PATH = STATE_DIR / "stories.db"
LOG_PATH = STATE_DIR / "youtube.log"

WATCH_LATER_URL = "https://www.youtube.com/playlist?list=WL"


def _resolve_yt_dlp() -> str | None:
    candidates = []
    python_bin = pathlib.Path(sys.executable)
    if python_bin.name.startswith("python"):
        candidates.append(str(python_bin.with_name("yt-dlp")))
    candidates.append(which("yt-dlp") or "")
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).exists():
            return candidate
    return None

def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")

def ensure_schema() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS external_bookmarks (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            excerpt TEXT,
            cover TEXT,
            author TEXT,
            created_ts INTEGER,
            scraped_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL
        )"""
    )
    cols = {r[1]
            for r in conn.execute("PRAGMA table_info(external_bookmarks)")}
    for col in (
        "avatar",
        "media_json",
        "quoted_json",
        "reply_to",
        "link_card_json",
        "tags",
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE external_bookmarks ADD COLUMN {col} TEXT")
    if "source_rank" not in cols:
        conn.execute("ALTER TABLE external_bookmarks ADD COLUMN source_rank INTEGER")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS external_item_state (
            id TEXT PRIMARY KEY,
            is_read INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    conn.close()

def export_cookies() -> None:
    from playwright.sync_api import sync_playwright

    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    print(f"Opening Playwright profile at {PROFILE_DIR}")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled"],
        )
        cookies = ctx.cookies()
        ctx.close()

    lines = [
        "# Netscape HTTP Cookie File",
        "# Exported from Playwright profile by ingest.youtube_sync",
        "",
    ]
    for c in cookies:
        domain = c["domain"]
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = 0
        name = c["name"]
        value = c["value"]
        lines.append(
            f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    COOKIES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(cookies)} cookies to {COOKIES_FILE}")

def fetch_watch_later(limit: int | None = None) -> list[dict]:
    if not COOKIES_FILE.exists():
        log("no cookies file — run with --export-cookies first")
        return []
    yt_dlp_bin = _resolve_yt_dlp()
    if not yt_dlp_bin:
        log("yt-dlp is not installed or not on PATH")
        return []
    cmd = [
        yt_dlp_bin,
        "--cookies", str(COOKIES_FILE),
        "--flat-playlist",
        "--dump-json",
        "--no-warnings",
        "--extractor-args", "youtube:player_skip=webpage,configs",
        WATCH_LATER_URL,
    ]
    if limit:
        cmd[-1:-1] = ["--playlist-end", str(limit)]
    log(f"running: {' '.join(cmd[:5])} ... {WATCH_LATER_URL}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        log("yt-dlp timed out")
        return []
    if proc.returncode != 0:
        log(f"yt-dlp failed (exit {proc.returncode}): {proc.stderr[:500]}")
        return []
    out = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

def upsert(videos: list[dict]) -> int:
    if not videos:
        return 0
    now = int(time.time())
    rows = []
    for v in videos:
        vid = v.get("id") or ""
        if not vid:
            continue
        ext_id = f"youtube:{vid}"
        url = v.get("url") or v.get(
            "webpage_url") or f"https://www.youtube.com/watch?v={vid}"
        title = (v.get("title") or "").strip() or "(untitled)"
        uploader = (v.get("uploader") or v.get("channel") or "").strip()
        playlist_index = int(v.get("playlist_index") or 0)

        thumb = ""
        thumbs = v.get("thumbnails") or []
        if thumbs:
            best = max(thumbs, key=lambda t: (
                t.get("width") or 0) * (t.get("height") or 0))
            thumb = best.get("url", "") or ""
        if not thumb:
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        duration = v.get("duration") or 0
        if duration:
            mm, ss = divmod(int(duration), 60)
            dur = f"{mm}:{ss:02d}"
            excerpt = f"{uploader} · {dur}" if uploader else dur
        else:
            excerpt = uploader or ""

        created_ts = now - max(playlist_index - 1, 0)
        rows.append(
            (
                ext_id, "youtube", url, title, excerpt, thumb, uploader, created_ts,
                now, now,
                "", "[]", None, "", None, "watch later", playlist_index,
            )
        )
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        """INSERT INTO external_bookmarks
               (id, source, url, title, excerpt, cover, author, created_ts,
                scraped_at, last_seen_at,
                avatar, media_json, quoted_json, reply_to, link_card_json, tags, source_rank)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title,
             excerpt=excluded.excerpt,
             cover=excluded.cover,
             author=excluded.author,
             tags=excluded.tags,
             source_rank=excluded.source_rank,
             last_seen_at=excluded.last_seen_at""",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)

def run() -> None:
    ensure_schema()
    videos = fetch_watch_later()
    log(f"fetched {len(videos)} videos from Watch Later")
    n = upsert(videos)
    log(f"upserted {n}")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--export-cookies",
        action="store_true",
        help="Read Google cookies from the Playwright profile and save to cookies.txt")
    args = ap.parse_args()
    if args.export_cookies:
        export_cookies()
        return
    run()

if __name__ == "__main__":
    main()

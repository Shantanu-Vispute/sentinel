import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from telethon.sync import TelegramClient
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from digest.media_cache import cache_bytes

_ERID_RE = re.compile(r"(?:[?&]|&amp;)erid=", re.IGNORECASE)

@dataclass
class TelegramPost:
    id: str
    channel: str
    msg_id: int
    url: str
    date: str
    text: str
    links: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    video_thumbs: list[str] = field(default_factory=list)
    is_sponsored: bool = False
    is_digest: bool = False

def _clean_link(href: str, channel: str) -> str | None:
    if not href:
        return None
    if href.startswith("?q="):
        return None
    if href.startswith("/"):
        return None
    host = urlsplit(href).netloc.lower()
    if host in ("t.me", "telegram.me", "telegram.dog"):
        return None
    return href

def _client() -> TelegramClient:
    from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH not set. Get them from "
            "https://my.telegram.org and add them to .env."
        )
    client = TelegramClient(TELEGRAM_SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    client.connect()
    if not client.is_user_authorized():
        client.disconnect()
        raise RuntimeError(
            "Telegram session not authorized. Run "
            "`python scripts/telegram-login.py` once to log in interactively."
        )
    return client

def _is_video_document(document) -> bool:
    return any(
        attr.__class__.__name__ == "DocumentAttributeVideo"
        for attr in (document.attributes or [])
    )

def _extract_links(message, channel: str) -> list[str]:
    links: list[str] = []
    for entity, text_piece in message.get_entities_text() or []:
        href = None
        if isinstance(entity, MessageEntityTextUrl):
            href = entity.url
        elif isinstance(entity, MessageEntityUrl):
            href = text_piece
        cleaned = _clean_link(href, channel) if href else None
        if cleaned and cleaned not in links:
            links.append(cleaned)

    webpage = getattr(getattr(message, "media", None), "webpage", None)
    webpage_url = getattr(webpage, "url", None)
    if webpage_url:
        cleaned = _clean_link(webpage_url, channel)
        if cleaned and cleaned not in links:
            links.insert(0, cleaned)
    return links

def _cache_message_media(client, channel: str, message, thumb=None) -> str:
    try:
        data = client.download_media(message, file=bytes, thumb=thumb)
    except Exception:
        return ""
    if not data:
        return ""
    key = f"tg:{channel}:{message.id}:{'thumb' if thumb is not None else 'photo'}"
    return cache_bytes(data, key=key, namespace="telegram", ext=".jpg")

def _message_to_post(client, channel: str, message) -> "TelegramPost | None":
    if message is None or message.action is not None:
        return None
    text = (message.raw_text or "").strip()
    if not text and not message.media:
        return None

    links = _extract_links(message, channel)
    is_sponsored = any(_ERID_RE.search(link) for link in links)
    unique_hosts = set()
    for link in links:
        host = urlsplit(link).netloc.lower().removeprefix("www.")
        if host and host not in ("x.com", "twitter.com"):
            unique_hosts.add(host)
    is_digest = len(unique_hosts) >= 3

    images: list[str] = []
    video_thumbs: list[str] = []
    if message.photo:
        cached = _cache_message_media(client, channel, message)
        if cached:
            images.append(cached)
    elif message.video or (message.document and _is_video_document(message.document)):
        cached = _cache_message_media(client, channel, message, thumb=-1)
        if cached:
            video_thumbs.append(cached)

    return TelegramPost(
        id=f"tg:{channel}:{message.id}",
        channel=channel,
        msg_id=message.id,
        url=f"https://t.me/{channel}/{message.id}",
        date=message.date.astimezone(timezone.utc).isoformat(),
        text=text,
        links=links,
        images=images,
        video_thumbs=video_thumbs,
        is_sponsored=is_sponsored,
        is_digest=is_digest,
    )

def _fetch_channel_posts(
        client: TelegramClient,
        channel: str,
        since: datetime | None = None,
        max_pages: int = 20) -> list[TelegramPost]:
    # Routine polls (since=None) only need the latest page, like the old scraper's
    # first-page-then-stop behavior. Only paginate deeper for explicit backfills.
    limit = min(max_pages * 20, 3000) if since else 20
    posts: list[TelegramPost] = []
    for message in client.iter_messages(channel, limit=limit):
        if since and message.date < since:
            break
        post = _message_to_post(client, channel, message)
        if post is not None:
            posts.append(post)
    posts.reverse()
    return posts

def fetch_channel(
        channel: str,
        since: datetime | None = None,
        max_pages: int = 20) -> list[TelegramPost]:
    client = _client()
    try:
        return _fetch_channel_posts(client, channel, since=since, max_pages=max_pages)
    finally:
        client.disconnect()

def fetch_channels(channels: list[str], since: datetime | None = None,
                   delay_seconds: float = 1.0) -> list[TelegramPost]:
    all_posts = []
    client = _client()
    try:
        for i, ch in enumerate(channels):
            try:
                posts = _fetch_channel_posts(client, ch, since=since)
                print(f"  @{ch}: {len(posts)} posts")
                all_posts.extend(posts)
            except Exception as e:
                print(f"  @{ch}: ERROR {e}")
            if i < len(channels) - 1:
                time.sleep(delay_seconds)
    finally:
        client.disconnect()
    return all_posts

if __name__ == "__main__":
    import sys
    chs = sys.argv[1:]
    if not chs:
        print("Usage: python -m ingest.telegram_fetcher channel_one channel_two")
        sys.exit(1)
    posts = fetch_channels(chs)
    print(f"\nTotal: {len(posts)} posts from {len(chs)} channel(s)")
    for p in posts[-3:]:
        marker = "🪧 AD" if p.is_sponsored else ""
        print(f"\n{p.url} {marker}")
        print(
            f"  {p.date} · {len(p.text)} chars · {len(p.links)} links · {len(p.images)} images")
        if p.text:
            print(f"  {p.text[:200]}...")

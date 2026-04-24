import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

_UA = "Mozilla/5.0 (Sentinel telegram_fetcher)"
_BG_URL_RE = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)")
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

def _extract_images(post_el) -> list[str]:
    out = []
    for ph in post_el.select(".tgme_widget_message_photo_wrap"):
        style = ph.get("style", "")
        m = _BG_URL_RE.search(style)
        if m:
            out.append(m.group(1))
    return out

def _extract_video_thumbs(post_el) -> list[str]:
    out = []
    for v in post_el.select(
        ".tgme_widget_message_video_thumb, .tgme_widget_message_video_wrap"
    ):
        style = v.get("style", "")
        m = _BG_URL_RE.search(style)
        if m:
            out.append(m.group(1))
    return out

def _parse_page(html: str, channel: str) -> list["TelegramPost"]:
    soup = BeautifulSoup(html, "html.parser")
    posts: list[TelegramPost] = []
    for wrap in soup.select(".tgme_widget_message_wrap"):
        date_el = wrap.select_one(".tgme_widget_message_date")
        if not date_el:
            continue
        msg_url = date_el.get("href", "")

        parts = urlsplit(msg_url).path.strip("/").split("/")
        if len(parts) < 2 or not parts[-1].isdigit():
            continue
        msg_id = int(parts[-1])

        time_el = wrap.select_one("time[datetime]")
        iso_date = time_el["datetime"] if time_el else datetime.now(
            timezone.utc).isoformat()

        text_el = wrap.select_one(".tgme_widget_message_text")
        text = text_el.get_text(separator="\n", strip=True) if text_el else ""

        links: list[str] = []
        if text_el:
            for a in text_el.select("a[href]"):
                cleaned = _clean_link(a["href"], channel)
                if cleaned and cleaned not in links:
                    links.append(cleaned)

        lp = wrap.select_one(".tgme_widget_message_link_preview[href]")
        if lp:
            cleaned = _clean_link(lp["href"], channel)
            if cleaned and cleaned not in links:
                links.insert(0, cleaned)

        images = _extract_images(wrap)
        video_thumbs = _extract_video_thumbs(wrap)

        is_sponsored = any(_ERID_RE.search(l) for l in links)

        unique_hosts = set()
        for l in links:
            host = urlsplit(l).netloc.lower().removeprefix("www.")
            if host and host not in ("x.com", "twitter.com"):
                unique_hosts.add(host)
        is_digest = len(unique_hosts) >= 3

        posts.append(TelegramPost(
            id=f"tg:{channel}:{msg_id}",
            channel=channel,
            msg_id=msg_id,
            url=msg_url,
            date=iso_date,
            text=text,
            links=links,
            images=images,
            video_thumbs=video_thumbs,
            is_sponsored=is_sponsored,
            is_digest=is_digest,
        ))
    return posts

def fetch_channel(
        channel: str,
        since: datetime | None = None,
        max_pages: int = 20,
        timeout: int = 15) -> list[TelegramPost]:
    base_url = f"https://t.me/s/{channel}"
    all_posts: list[TelegramPost] = []
    seen_ids: set[int] = set()
    before: int | None = None

    for page_idx in range(max_pages):
        url = base_url if before is None else f"{base_url}?before={before}"
        r = requests.get(url, timeout=timeout, headers={"User-Agent": _UA})
        r.raise_for_status()
        page_posts = _parse_page(r.text, channel)
        if not page_posts:
            break

        new_this_page = [p for p in page_posts if p.msg_id not in seen_ids]
        if not new_this_page:
            break
        for p in new_this_page:
            seen_ids.add(p.msg_id)
        all_posts.extend(new_this_page)

        oldest = min(page_posts, key=lambda p: p.msg_id)

        if since:
            try:
                oldest_dt = datetime.fromisoformat(oldest.date)
                if oldest_dt < since:
                    break
            except Exception:
                pass
        else:
            break

        before = oldest.msg_id
        time.sleep(0.8)

    if since:
        filtered = []
        for p in all_posts:
            try:
                if datetime.fromisoformat(p.date) >= since:
                    filtered.append(p)
            except Exception:
                filtered.append(p)
        return filtered
    return all_posts

def fetch_channels(channels: list[str], since: datetime | None = None,
                   delay_seconds: float = 1.0) -> list[TelegramPost]:
    all_posts = []
    for i, ch in enumerate(channels):
        try:
            posts = fetch_channel(ch, since=since)
            print(f"  @{ch}: {len(posts)} posts")
            all_posts.extend(posts)
        except Exception as e:
            print(f"  @{ch}: ERROR {e}")
        if i < len(channels) - 1:
            time.sleep(delay_seconds)
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

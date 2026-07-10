import re

import requests

from digest.link_extractor import extract_links_from_html

_UA = "Mozilla/5.0 (Sentinel X-link extractor)"

_TWEET_URL_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/(\w{1,20})/status/(\d+)",
    re.IGNORECASE,
)
# X's own share button produces "/i/web/status/<id>" when it doesn't resolve
# a handle inline — a legitimate, fairly common tweet-link shape distinct
# from the normal "/<handle>/status/<id>" form above.
_TWEET_URL_NO_HANDLE_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/i/web/status/(\d+)",
    re.IGNORECASE,
)


def canonical_tweet_url(url: str) -> tuple[str, str] | None:
    """Returns (canonical_url, tweet_id) if `url` is an x.com/twitter.com
    status link, else None. Canonicalizes to x.com so twitter.com/x.com
    variants of the same tweet dedupe to one entry."""
    url = (url or "").strip()
    m = _TWEET_URL_RE.match(url)
    if m:
        handle, tweet_id = m.group(1), m.group(2)
        return f"https://x.com/{handle}/status/{tweet_id}", tweet_id
    m = _TWEET_URL_NO_HANDLE_RE.match(url)
    if m:
        tweet_id = m.group(1)
        return f"https://x.com/i/web/status/{tweet_id}", tweet_id
    return None


def find_x_links(links: list) -> list[dict]:
    """Scan a list of hrefs (either plain strings, e.g. Telegram's
    TelegramPost.links, or dicts with an "href" key, e.g.
    extract_links_from_html()'s output) for embedded X/Twitter post links."""
    seen_ids = set()
    out = []
    for link in links or []:
        href = link.get("href") if isinstance(link, dict) else link
        canonical = canonical_tweet_url(href or "")
        if canonical is None:
            continue
        url, tweet_id = canonical
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        out.append({"url": url, "tweet_id": tweet_id})
    return out


def fetch_page_html(url: str, timeout: float = 10.0) -> str | None:
    """Fetch an arbitrary external URL's HTML. Returns None on any failure —
    callers should treat this as a best-effort enrichment step, not a hard
    dependency."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        r = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": _UA},
            allow_redirects=True,
        )
        r.raise_for_status()
        content_type = (r.headers.get("content-type") or "").lower()
        if "html" not in content_type and content_type:
            return None
        return r.text
    except Exception:
        return None


def find_x_links_in_page(url: str, timeout: float = 10.0) -> list[dict]:
    """Fetch `url` and extract any embedded X/Twitter post links from its
    HTML — the "one hop deeper" step for stories whose primary link points
    at a blog/article that itself cites/embeds tweets."""
    html = fetch_page_html(url, timeout=timeout)
    if not html:
        return []
    return find_x_links(extract_links_from_html(html))

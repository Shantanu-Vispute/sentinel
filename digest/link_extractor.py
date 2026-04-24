import re
from urllib.parse import urlsplit

import requests

_A_TAG_RE = re.compile(
    r'<a\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_INNER_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_BAD_URL_SUBSTRINGS = (
    "unsubscribe",
    "list-manage",
    "manage-subscriptions",
    "manage-preferences",
    "manage_subscriptions",
    "manage-preferences",
    "update-profile",
    "update_profile",
    "forward-this",
    "forward-to",
    "view-in-browser",
    "view_in_browser",
    "preferences",
    "archive/",
    "/profile/",
    "sendgrid.net",
    "mailchimp",
    "mailchi.mp",
    "mail.google.com",
    "substackcdn",
    "list.robinhood",
)

_WRAPPER_HOST_PREFIXES = (
    "click.", "link.", "links.", "email.", "e.", "track.", "tracking.",
    "r.", "t.", "c.", "go.", "mail.", "ml.",
)

_SHORTENER_HOSTS = {
    "t.co", "bit.ly", "lnkd.in", "lnk.in", "ow.ly", "tinyurl.com",
    "tr.ee", "buff.ly", "is.gd", "shorturl.at", "rebrand.ly",
}

def _host_of(url: str) -> str:
    try:
        h = urlsplit(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def _is_wrapper_host(host: str) -> bool:
    return any(host.startswith(p) for p in _WRAPPER_HOST_PREFIXES)

def _is_bad_url(url: str, sender_host: str = "") -> bool:
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return True
    for sub in _BAD_URL_SUBSTRINGS:
        if sub in low:
            return True
    host = _host_of(low)
    if sender_host and host and (
            host == sender_host or host.endswith(
            "." + sender_host)):
        return True
    return False

def extract_links_from_html(html: str) -> list[dict]:
    if not html:
        return []
    html = re.sub(
        r"<(style|script)[^>]*>[\s\S]*?</\1>",
        "",
        html,
        flags=re.IGNORECASE)

    out = []
    for m in _A_TAG_RE.finditer(html):
        href = (m.group(1) or "").strip()
        anchor = _INNER_TAG_RE.sub(" ", m.group(2) or "").strip()
        anchor = _WS_RE.sub(" ", anchor)
        if not href:
            continue

        before = _WS_RE.sub(" ", _INNER_TAG_RE.sub(
            " ", html[max(0, m.start() - 1500): m.start()]))
        after = _WS_RE.sub(" ", _INNER_TAG_RE.sub(
            " ", html[m.end(): m.end() + 1500]))
        context = (before[-300:] + " " + anchor + " " + after[:300]).strip()
        out.append({"anchor": anchor, "href": href, "context": context})
    return out

def unshorten(url: str, timeout: float = 6.0, max_hops: int = 2) -> str:
    current = url
    for _ in range(max_hops):
        host = _host_of(current)
        if host not in _SHORTENER_HOSTS and not _is_wrapper_host(host):
            return current
        resolved = None
        for method in ("HEAD", "GET"):
            try:
                if method == "HEAD":
                    r = requests.head(
                        current, allow_redirects=True, timeout=timeout)
                else:
                    r = requests.get(
                        current,
                        allow_redirects=True,
                        timeout=timeout,
                        stream=True)
                    r.close()
                if r.url and r.url != current:
                    resolved = r.url
                    break
            except Exception:
                continue
        if resolved is None:
            return current
        current = resolved
    return current

def _word_set(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2}

def pick_primary_url(
    story_title: str,
    story_summary: str,
    links: list[dict],
    sender_host: str = "",
    min_weighted_score: int = 2,
) -> str:
    if not links:
        return ""
    text_tokens = _word_set(story_title + " " + (story_summary or "")[:220])
    if not text_tokens:
        return ""

    best_url, best_score, best_anchor_len = "", 0, 0
    for link in links:
        href = link.get("href") or ""
        if _is_bad_url(href, sender_host):
            continue
        anchor_tokens = _word_set(link.get("anchor") or "")
        ctx_tokens = _word_set(link.get("context") or "")
        score = 3 * len(text_tokens & anchor_tokens) + \
            len(text_tokens & ctx_tokens)
        if score < min_weighted_score:
            continue
        alen = len(link.get("anchor") or "")
        if score > best_score or (
                score == best_score and alen > best_anchor_len):
            best_score, best_url, best_anchor_len = score, href, alen

    if not best_url:
        return ""

    resolved = unshorten(best_url)
    resolved_host = _host_of(resolved)
    if _is_bad_url(resolved, sender_host):
        return ""
    if _is_wrapper_host(resolved_host):
        return ""
    return resolved

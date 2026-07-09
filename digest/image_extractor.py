import re

from digest.link_extractor import _word_set

_IMG_TAG_RE = re.compile(r"<img\s+[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r'(\w[\w-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'=<>`]+))',
    re.IGNORECASE,
)
_INNER_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_TRACKING_URL_MARKERS = (
    "tracking.", "track.", "/o?", "/ss/o/", "beacon", "open.gif",
    "pixel", "/ci0/", "/CI0/",
)


def _parse_attrs(tag: str) -> dict[str, str]:
    attrs = {}
    for m in _ATTR_RE.finditer(tag):
        name = m.group(1).lower()
        value = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4))
        attrs[name] = value or ""
    return attrs


def _to_int(value: str) -> int | None:
    try:
        return int(re.sub(r"[^\d]", "", value or "") or "0") or None
    except ValueError:
        return None


def _is_tracking_pixel(src: str, width: int | None, height: int | None) -> bool:
    if width is not None and width <= 2:
        return True
    if height is not None and height <= 2:
        return True
    low = (src or "").lower()
    return any(marker in low for marker in _TRACKING_URL_MARKERS)


def _looks_like_branding(img: dict) -> bool:
    alt = (img.get("alt") or "").lower()
    if any(kw in alt for kw in ("logo", "sponsor", "partner", "icon", "avatar", "author photo")):
        return True
    # Real story photos in these templates are essentially always raster
    # images (png/jpg/webp); a bare .gif is almost always a spacer, open
    # tracker, or animated ad banner that _is_tracking_pixel didn't catch
    # (e.g. no explicit width/height attributes to key off of).
    return img["src"].split("?")[0].lower().endswith(".gif")


def extract_images_from_html(html: str) -> list[dict]:
    """Parse <img> tags into candidates (src, alt, dimensions, and the
    surrounding text as `context`, mirroring extract_links_from_html), for
    matching a specific story's image the same way pick_primary_url matches
    a specific story's link — not "the one image for the whole email"."""
    if not html:
        return []
    html = re.sub(r"<(style|script)[^>]*>[\s\S]*?</\1>", "", html, flags=re.IGNORECASE)

    out = []
    for m in _IMG_TAG_RE.finditer(html):
        tag = m.group(0)
        attrs = _parse_attrs(tag)
        src = (attrs.get("src") or "").strip()
        if not src or not src.lower().startswith(("http://", "https://")):
            continue
        width = _to_int(attrs.get("width", ""))
        height = _to_int(attrs.get("height", ""))
        if _is_tracking_pixel(src, width, height):
            continue

        before = _INNER_TAG_RE.sub(" ", html[max(0, m.start() - 800): m.start()])
        after = _INNER_TAG_RE.sub(" ", html[m.end(): m.end() + 800])
        context = _WS_RE.sub(" ", (before[-300:] + " " + after[:300])).strip()

        out.append({
            "src": src,
            "alt": (attrs.get("alt") or "").strip(),
            "width": width,
            "height": height,
            "context": context,
        })
    return out


def pick_primary_image(
        story_title: str,
        story_summary: str,
        images: list[dict],
        min_weighted_score: int = 2) -> str:
    """Pick the image whose alt text/surrounding context best matches this
    specific story, the same way pick_primary_url scores links. Returns ""
    if nothing scores well enough — a curated_digest email with 5 stories
    and 2 real content images should give 3 stories no image, not a wrong one."""
    if not images:
        return ""
    text_tokens = _word_set(story_title + " " + (story_summary or "")[:220])
    if not text_tokens:
        return ""

    candidates = [img for img in images if not _looks_like_branding(img)]
    if not candidates:
        return ""

    best_src, best_score, best_width = "", 0, 0
    for img in candidates:
        alt_tokens = _word_set(img.get("alt") or "")
        ctx_tokens = _word_set(img.get("context") or "")
        score = 3 * len(text_tokens & alt_tokens) + len(text_tokens & ctx_tokens)
        if score < min_weighted_score:
            continue
        width = img.get("width") or 0
        if score > best_score or (score == best_score and width > best_width):
            best_score, best_src, best_width = score, img["src"], width

    return best_src

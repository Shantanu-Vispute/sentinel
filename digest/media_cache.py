import hashlib
import mimetypes
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError


STATE_DIR = Path(__file__).resolve().parent.parent / "state"
MEDIA_DIR = STATE_DIR / "media"
_UA = "Mozilla/5.0 (Sentinel media cache)"
_ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

# Story cards render a 369x160px cover crop (see .card-thumb in
# templates/digest_list.html); anything smaller than this is an icon/avatar/
# logo, not a real cover photo, and looks blurry/pixelated when stretched to
# fill the card. HTML-declared width/height attributes are unreliable (often
# missing, sometimes stale), so this checks the actual downloaded pixels —
# the one authoritative point every image source (email, Telegram, future
# sources) funnels through.
MIN_IMAGE_WIDTH = 300
MIN_IMAGE_HEIGHT = 150


def _meets_min_resolution(data: bytes) -> bool:
    try:
        with Image.open(BytesIO(data)) as img:
            width, height = img.size
    except (UnidentifiedImageError, OSError):
        # Can't verify (corrupt/unsupported format) — reject rather than
        # risk showing something broken or tiny on a story card.
        return False
    return width >= MIN_IMAGE_WIDTH and height >= MIN_IMAGE_HEIGHT


def is_local_media_url(url: str) -> bool:
    return (url or "").startswith("/media/")


def cache_remote_image(url: str, namespace: str = "telegram") -> str:
    """Download a remote image into state/media and return its app-local URL.

    Returns "" if the image is below the minimum story-card resolution."""
    if not url or is_local_media_url(url):
        return url or ""

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    target_dir = MEDIA_DIR / namespace
    target_dir.mkdir(parents=True, exist_ok=True)

    existing = next(target_dir.glob(f"{digest}.*"), None)
    if existing:
        return f"/media/{namespace}/{existing.name}"

    r = requests.get(url, timeout=20, headers={"User-Agent": _UA})
    r.raise_for_status()

    if not _meets_min_resolution(r.content):
        return ""

    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].lower()
    ext = _ALLOWED_TYPES.get(content_type)
    if ext is None:
        guessed = mimetypes.guess_extension(content_type or "")
        ext = guessed if guessed in _ALLOWED_TYPES.values() else ".jpg"

    target = target_dir / f"{digest}{ext}"
    target.write_bytes(r.content)
    return f"/media/{namespace}/{target.name}"


def cache_bytes(data: bytes, key: str, namespace: str = "telegram", ext: str = ".jpg") -> str:
    """Store already-downloaded media bytes into state/media and return its
    app-local URL. Returns "" if the image is below the minimum story-card
    resolution."""
    if not data:
        return ""

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    target_dir = MEDIA_DIR / namespace
    target_dir.mkdir(parents=True, exist_ok=True)

    existing = next(target_dir.glob(f"{digest}.*"), None)
    if existing:
        return f"/media/{namespace}/{existing.name}"

    if not _meets_min_resolution(data):
        return ""

    if ext not in _ALLOWED_TYPES.values():
        ext = ".jpg"
    target = target_dir / f"{digest}{ext}"
    target.write_bytes(data)
    return f"/media/{namespace}/{target.name}"


def media_file_path(rel_path: str) -> Path | None:
    rel = (rel_path or "").strip("/")
    if not rel:
        return None
    path = (MEDIA_DIR / rel).resolve()
    try:
        path.relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return None
    return path


def remote_host(url: str) -> str:
    return urlsplit(url or "").netloc.lower()

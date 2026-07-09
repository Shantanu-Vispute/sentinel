import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import requests


STATE_DIR = Path(__file__).resolve().parent.parent / "state"
MEDIA_DIR = STATE_DIR / "media"
_UA = "Mozilla/5.0 (Sentinel media cache)"
_ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def is_local_media_url(url: str) -> bool:
    return (url or "").startswith("/media/")


def cache_remote_image(url: str, namespace: str = "telegram") -> str:
    """Download a remote image into state/media and return its app-local URL."""
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

    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].lower()
    ext = _ALLOWED_TYPES.get(content_type)
    if ext is None:
        guessed = mimetypes.guess_extension(content_type or "")
        ext = guessed if guessed in _ALLOWED_TYPES.values() else ".jpg"

    target = target_dir / f"{digest}{ext}"
    target.write_bytes(r.content)
    return f"/media/{namespace}/{target.name}"


def cache_bytes(data: bytes, key: str, namespace: str = "telegram", ext: str = ".jpg") -> str:
    """Store already-downloaded media bytes into state/media and return its app-local URL."""
    if not data:
        return ""

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    target_dir = MEDIA_DIR / namespace
    target_dir.mkdir(parents=True, exist_ok=True)

    existing = next(target_dir.glob(f"{digest}.*"), None)
    if existing:
        return f"/media/{namespace}/{existing.name}"

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

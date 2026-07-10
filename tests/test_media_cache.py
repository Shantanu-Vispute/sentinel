import shutil
from io import BytesIO

import pytest
from PIL import Image

from digest.media_cache import MEDIA_DIR, _meets_min_resolution, cache_bytes


def _png_bytes(width: int, height: int) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (width, height), color="red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _cleanup_test_namespace():
    yield
    shutil.rmtree(MEDIA_DIR / "test_media_cache", ignore_errors=True)


def test_meets_min_resolution_rejects_small_icon():
    assert _meets_min_resolution(_png_bytes(60, 60)) is False


def test_meets_min_resolution_rejects_narrow_banner():
    # Wide enough but too short — a thin banner/divider, not a cover photo.
    assert _meets_min_resolution(_png_bytes(600, 40)) is False


def test_meets_min_resolution_accepts_content_sized_image():
    assert _meets_min_resolution(_png_bytes(500, 300)) is True


def test_meets_min_resolution_rejects_corrupt_data():
    assert _meets_min_resolution(b"not an image") is False


def test_cache_bytes_returns_empty_string_for_small_image():
    result = cache_bytes(_png_bytes(60, 60), key="tiny-icon", namespace="test_media_cache")
    assert result == ""
    namespace_dir = MEDIA_DIR / "test_media_cache"
    assert not namespace_dir.exists() or not any(namespace_dir.glob("*"))


def test_cache_bytes_caches_content_sized_image():
    result = cache_bytes(_png_bytes(500, 300), key="real-photo", namespace="test_media_cache")
    assert result.startswith("/media/test_media_cache/")
    assert result.endswith(".jpg")

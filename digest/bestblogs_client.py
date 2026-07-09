from __future__ import annotations

from typing import Any

import requests

from config import BESTBLOGS_API_BASE


CATEGORY_ALIASES = {
    "programming": "Programming_Technology",
    "ai": "Artificial_Intelligence",
    "product": "Product_Development",
    "business": "Business_Tech",
    "growth": "Productivity_Growth",
    "new": "News_Media",
    "news": "News_Media",
    "finance": "Finance_Economy",
    "lifestyle": "Lifestyle_Culture",
    "sports": "Sports",
}

# The API's real resourceType value for tweets is "TWITTER", not "TWEET" —
# confirmed against live data (state/bestblogs_samples/type__ALL.json).
RESOURCE_TYPES = {"ALL", "ARTICLE", "VIDEO", "PODCAST", "TWITTER"}
TIME_FILTERS = {"all", "1d", "3d", "1w", "1m", "3m", "1y"}
SORT_TYPES = {"default", "time_desc", "score_desc", "read_desc"}
LANGUAGES = {"all", "en", "zh", "en_US", "zh_CN"}


def fetch_resources(params: dict[str, Any]) -> dict:
    clean = _resource_params(params)
    return _get_json("/resources", clean)


def fetch_categories() -> dict:
    return _get_json("/categories", {})


def fetch_source_options(resource_type: str = "ALL", page: int = 1, page_size: int = 100) -> dict:
    resource_type = _resource_type(resource_type)
    return _get_json(
        "/sources/options",
        {
            "resourceType": resource_type,
            "page": max(int(page or 1), 1),
            "pageSize": min(max(int(page_size or 100), 1), 100),
        },
    )


def _resource_params(params: dict[str, Any]) -> dict[str, Any]:
    page = _int(params.get("page"), 1)
    page_size = min(max(_int(params.get("pageSize") or params.get("page_size"), 10), 1), 100)
    out: dict[str, Any] = {
        "page": max(page, 1),
        "pageSize": page_size,
        "timeFilter": _choice(params.get("timeFilter") or params.get("time_filter"), TIME_FILTERS, "1w"),
        "language": _choice(params.get("language"), LANGUAGES, "all"),
        "sortType": _sort_type(params.get("sortType") or params.get("sort")),
        "type": _resource_type(params.get("type")),
        "uiLang": params.get("uiLang") or "en",
    }
    category = _category(params.get("category"))
    if category:
        out["category"] = category
    source_id = _source_id(params.get("sourceId") or params.get("sourceid"))
    if source_id:
        out["sourceId"] = source_id
    for key in ("lowerTotalScore", "upperTotalScore"):
        value = params.get(key)
        if value not in (None, ""):
            out[key] = _int(value, 0)
    return out


def _get_json(path: str, params: dict[str, Any]) -> dict:
    response = requests.get(
        f"{BESTBLOGS_API_BASE}{path}",
        params=params,
        headers={
            "accept": "application/json",
            "user-agent": "Sentinel/BestBlogsExplore",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success", False):
        message = data.get("message") or "BestBlogs API request failed"
        raise RuntimeError(message)
    return data


def _category(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "all":
        return ""
    return CATEGORY_ALIASES.get(raw, raw)


def _source_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw if raw.startswith("SOURCE_") else f"SOURCE_{raw}"


def _sort_type(value: Any) -> str:
    raw = str(value or "").strip()
    if raw == "time":
        raw = "time_desc"
    if raw == "score":
        raw = "score_desc"
    if raw == "read":
        raw = "read_desc"
    return _choice(raw, SORT_TYPES, "time_desc")


def _resource_type(value: Any) -> str:
    raw = str(value or "").upper()
    if raw == "TWEET":
        raw = "TWITTER"
    return _choice(raw, RESOURCE_TYPES, "ARTICLE")


def _choice(value: Any, allowed: set[str], default: str) -> str:
    raw = str(value or "").strip()
    return raw if raw in allowed else default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

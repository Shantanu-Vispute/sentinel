#!/usr/bin/env python3
"""One-off exploration: dump BestBlogs /api/proxy responses across param
variations so we can inspect what data each parameter actually changes.

Strategy: one baseline call with sane defaults, then vary ONE parameter at
a time from that baseline (holding everything else fixed) — cheap enough to
run in full (~30 requests) while still showing what each param controls.

Writes JSON files into state/bestblogs_samples/ (gitignored, local only).
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from digest.bestblogs_client import (
    CATEGORY_ALIASES,
    LANGUAGES,
    RESOURCE_TYPES,
    SORT_TYPES,
    TIME_FILTERS,
    fetch_categories,
    fetch_resources,
    fetch_source_options,
)

OUT_DIR = ROOT / "state" / "bestblogs_samples"

BASELINE = {
    "page": 1,
    "pageSize": 10,
    "timeFilter": "1w",
    "language": "all",
    "sortType": "time_desc",
    "type": "ARTICLE",
    "category": "ai",
}


def _dump(name: str, payload: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    data = payload.get("data")
    if isinstance(data, dict):
        data_list = data.get("dataList")
        count = len(data_list) if isinstance(data_list, list) else "-"
    elif isinstance(data, list):
        count = len(data)
    else:
        count = "-"
    print(f"  {name:<40} items={count!s:<5} -> {path.relative_to(ROOT)}")


def _try(name: str, params: dict) -> None:
    try:
        payload = fetch_resources(params)
        _dump(name, payload)
    except Exception as exc:
        print(f"  {name:<40} ERROR: {exc}")
    time.sleep(0.3)


def main():
    print(f"Writing samples to {OUT_DIR}\n")

    print("== categories & sources (once each) ==")
    _dump("categories", fetch_categories())
    _dump("sources_options_all", fetch_source_options(resource_type="ALL", page=1, page_size=100))

    print("\n== baseline ==")
    _try("baseline", BASELINE)

    print("\n== varying: category ==")
    for alias in list(CATEGORY_ALIASES) + ["all"]:
        params = {**BASELINE, "category": alias}
        _try(f"category__{alias}", params)

    print("\n== varying: sortType ==")
    for sort_type in SORT_TYPES:
        params = {**BASELINE, "sortType": sort_type}
        _try(f"sortType__{sort_type}", params)

    print("\n== varying: timeFilter ==")
    for time_filter in TIME_FILTERS:
        params = {**BASELINE, "timeFilter": time_filter}
        _try(f"timeFilter__{time_filter}", params)

    print("\n== varying: type (resourceType) ==")
    for resource_type in RESOURCE_TYPES:
        params = {**BASELINE, "type": resource_type}
        _try(f"type__{resource_type}", params)

    print("\n== varying: language ==")
    for language in LANGUAGES:
        params = {**BASELINE, "language": language}
        _try(f"language__{language}", params)

    print("\n== score bounds ==")
    _try("score__lower80", {**BASELINE, "lowerTotalScore": 80})
    _try("score__upper50", {**BASELINE, "upperTotalScore": 50})

    print("\nDone.")


if __name__ == "__main__":
    main()

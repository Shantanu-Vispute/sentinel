import json
import re
from dataclasses import dataclass
from pathlib import Path

import config


_SPACE_RE = re.compile(r"\s+")
_QUOTE_RE = re.compile(r"^[\"'\u201c\u201d]+|[\"'\u201c\u201d]+$")


def _load_sender_signal_config(path: str) -> dict:
    """Load user-defined newsletter trust tables.

    This file is intentionally not checked into git (see .gitignore's
    `state/` entry): the "which senders do I trust" list is a personal
    preference, not something this project should ship an opinion on.
    See sender_signals.example.json for the expected format.
    """
    try:
        raw = Path(path).read_text()
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


_CONFIG = _load_sender_signal_config(config.SENDER_SIGNALS_PATH)

PRIMARY_EARLY_AI_SENDERS: dict[str, float] = _CONFIG.get("trusted_early_ai", {})
SCOUT_AI_SENDERS: dict[str, float] = _CONFIG.get("scout_ai", {})
SUPPORTING_AI_SENDERS: dict[str, float] = _CONFIG.get("supporting_ai", {})
NOISY_FAST_SENDERS: set[str] = set(_CONFIG.get("noisy_fast", []))
DEV_LANE_SENDERS: set[str] = set(_CONFIG.get("dev_lane", []))
ITEM_FEED_SENDERS: set[str] = set(_CONFIG.get("item_feed", []))


@dataclass(frozen=True)
class SenderSignal:
    score: float
    label: str
    trusted_early_count: int = 0
    scout_count: int = 0
    supporting_count: int = 0
    noisy_fast_count: int = 0
    dev_lane_count: int = 0
    item_feed_count: int = 0

    @property
    def has_trusted_ai_signal(self) -> bool:
        return bool(self.trusted_early_count or self.scout_count)

    @property
    def is_noisy_only(self) -> bool:
        return (
            self.noisy_fast_count > 0
            and self.trusted_early_count == 0
            and self.scout_count == 0
            and self.supporting_count == 0
        )

    @property
    def is_dev_lane_only(self) -> bool:
        return (
            self.dev_lane_count > 0
            and self.trusted_early_count == 0
            and self.scout_count == 0
            and self.supporting_count == 0
        )


def normalize_sender(sender: str) -> str:
    value = _QUOTE_RE.sub("", (sender or "").strip()).lower()
    value = value.replace("\u2019", "'").replace("\u2014", "-")
    value = _SPACE_RE.sub(" ", value)
    return value


def sender_signal_for_story(
    senders: list[str] | tuple[str, ...],
    *,
    source_type: str = "email",
) -> SenderSignal:
    """Return a deterministic ranking signal for known newsletter source quality."""
    if (source_type or "email").strip().lower() != "email":
        return SenderSignal(score=0.0, label="")

    normalized = {
        normalize_sender(sender)
        for sender in senders
        if normalize_sender(sender)
    }
    if not normalized:
        return SenderSignal(score=0.0, label="")

    trusted_score = 0.0
    scout_score = 0.0
    supporting_score = 0.0
    trusted_count = scout_count = supporting_count = 0
    noisy_count = dev_count = feed_count = 0

    for sender in normalized:
        if sender in PRIMARY_EARLY_AI_SENDERS:
            trusted_count += 1
            trusted_score += PRIMARY_EARLY_AI_SENDERS[sender]
        elif sender in SCOUT_AI_SENDERS:
            scout_count += 1
            scout_score += SCOUT_AI_SENDERS[sender]
        elif sender in SUPPORTING_AI_SENDERS:
            supporting_count += 1
            supporting_score += SUPPORTING_AI_SENDERS[sender]
        elif sender in NOISY_FAST_SENDERS:
            noisy_count += 1
        elif sender in DEV_LANE_SENDERS:
            dev_count += 1
        elif sender in ITEM_FEED_SENDERS:
            feed_count += 1

    score = (
        min(trusted_score, 4.2)
        + min(scout_score, 1.8)
        + min(supporting_score, 0.8)
        + min(noisy_count * 0.15, 0.3)
    )

    label = ""
    if trusted_count:
        label = "trusted_early_ai"
    elif scout_count:
        label = "ai_scout"
    elif supporting_count:
        label = "supporting_ai"
    elif noisy_count:
        label = "noisy_fast"
        score -= 0.8
    elif dev_count:
        label = "dev_lane"
        score -= 2.0
    elif feed_count:
        label = "item_feed"
        score -= 0.4

    if dev_count and not (trusted_count or scout_count or supporting_count):
        score -= 1.0
    if feed_count and not (trusted_count or scout_count or supporting_count):
        score -= 0.2

    return SenderSignal(
        score=score,
        label=label,
        trusted_early_count=trusted_count,
        scout_count=scout_count,
        supporting_count=supporting_count,
        noisy_fast_count=noisy_count,
        dev_lane_count=dev_count,
        item_feed_count=feed_count,
    )


def is_trusted_early_ai_sender(sender: str) -> bool:
    signal = sender_signal_for_story([sender], source_type="email")
    return signal.has_trusted_ai_signal

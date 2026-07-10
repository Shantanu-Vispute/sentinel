from unittest.mock import patch

from digest.sender_signal import SenderSignal
from digest.story_quality import assess_story_quality


def _dev_lane_signal():
    return SenderSignal(score=-2.0, label="dev_lane", dev_lane_count=1)


def test_dev_lane_sender_with_strong_ai_content_is_not_skipped():
    """A dev-lane-classified sender (e.g. a general web-dev newsletter) can
    still cover a genuinely AI-relevant item — content relevance should win
    over sender classification, matching the escape hatch library_release
    already has via has_strong_ai."""
    with patch("digest.story_quality.sender_signal_for_story", return_value=_dev_lane_signal()):
        quality = assess_story_quality(
            title="Some New Agentic Patterns",
            summary=(
                "This piece explores emerging agentic design patterns for LLM-based "
                "systems, covering tool use, multi-agent orchestration, and inference-time "
                "reasoning strategies used by modern foundation models."
            ),
            source_type="email",
            sender="dev-newsletter@example.com",
        )

    assert quality.should_skip_ingestion is False
    assert quality.reason != "dev_lane_sender"


def test_dev_lane_sender_with_generic_content_is_still_skipped():
    """Generic dev-tooling content from a dev-lane sender should still be
    filtered — the fix only adds an escape hatch for strong AI relevance,
    it doesn't disable the filter."""
    with patch("digest.story_quality.sender_signal_for_story", return_value=_dev_lane_signal()):
        quality = assess_story_quality(
            title="pnpm 11.5 Update",
            summary=(
                "The pnpm package manager has released version 11.5 with faster "
                "installs, improved workspace support, and various bug fixes for "
                "monorepo dependency resolution."
            ),
            source_type="email",
            sender="dev-newsletter@example.com",
        )

    assert quality.should_skip_ingestion is True
    assert quality.reason == "dev_lane_sender"

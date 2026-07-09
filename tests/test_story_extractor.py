from unittest.mock import MagicMock, patch

from digest.gmail_fetcher import Newsletter
from digest.story_extractor import extract_stories


def _newsletter(**overrides) -> Newsletter:
    defaults = dict(
        id="test1",
        subject="Test Newsletter",
        sender="sender@example.com",
        date="2026-07-10T00:00:00Z",
        body="Some AI news body content here.",
        snippet="snippet",
        gmail_url="https://mail.google.com/x",
    )
    defaults.update(overrides)
    return Newsletter(**defaults)


def test_extraction_exception_signals_failure_not_empty_result():
    nl = _newsletter()
    with patch("digest.story_extractor._is_promotional", return_value=False), \
         patch("digest.story_extractor._is_tool_directory_digest", return_value=False), \
         patch(
             "digest.story_extractor._extract_from_single",
             side_effect=RuntimeError("All Gemini chat models/keys were rate-limited"),
         ):
        stories, failed = extract_stories([nl])

    assert stories == []
    assert failed is True


def test_genuinely_empty_result_does_not_signal_failure():
    nl = _newsletter()
    with patch("digest.story_extractor._is_promotional", return_value=False), \
         patch("digest.story_extractor._is_tool_directory_digest", return_value=False), \
         patch("digest.story_extractor._extract_from_single", return_value=[]):
        stories, failed = extract_stories([nl])

    assert stories == []
    assert failed is False


def test_promotional_skip_does_not_signal_failure():
    nl = _newsletter()
    with patch("digest.story_extractor._is_promotional", return_value=True):
        stories, failed = extract_stories([nl])

    assert stories == []
    assert failed is False


def test_successful_extraction_returns_stories_and_no_failure():
    nl = _newsletter()
    fake_stories = [
        MagicMock(title="Story One", summary="Summary one"),
        MagicMock(title="Story Two", summary="Summary two"),
    ]
    with patch("digest.story_extractor._is_promotional", return_value=False), \
         patch("digest.story_extractor._is_tool_directory_digest", return_value=False), \
         patch("digest.story_extractor._extract_from_single", return_value=fake_stories):
        stories, failed = extract_stories([nl])

    assert stories == fake_stories
    assert failed is False


def test_one_failure_among_several_newsletters_still_signals_failure():
    good = _newsletter(id="good")
    bad = _newsletter(id="bad")
    ok_story = MagicMock(title="OK Story", summary="This one worked")

    def side_effect(newsletter):
        if newsletter.id == "bad":
            raise RuntimeError("boom")
        return [ok_story]

    with patch("digest.story_extractor._is_promotional", return_value=False), \
         patch("digest.story_extractor._is_tool_directory_digest", return_value=False), \
         patch("digest.story_extractor._extract_from_single", side_effect=side_effect):
        stories, failed = extract_stories([good, bad])

    assert stories == [ok_story]
    assert failed is True

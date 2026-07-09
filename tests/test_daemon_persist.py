from unittest.mock import MagicMock, patch

from digest.daemon import _persist_story


def _story(**overrides):
    defaults = dict(
        title="OpenAI ships GPT-5.6",
        summary="A summary here",
        source_sender="sender@example.com",
        source_gmail_url="https://mail.google.com/x",
        source_newsletter="Newsletter",
        source_email_body="body",
        date="2026-07-10T00:00:00Z",
        primary_url="https://example.com",
    )
    defaults.update(overrides)
    return MagicMock(**defaults)


def _quality(should_skip_ingestion=False, reason=""):
    return MagicMock(should_skip_ingestion=should_skip_ingestion, reason=reason)


def test_embed_failure_returns_ok_false():
    db = MagicMock()
    with patch("digest.daemon.assess_story_quality", return_value=_quality()), \
         patch("digest.daemon.llm_client.embed", side_effect=RuntimeError("quota exceeded")):
        new_count, merged_count, evolved_count, ok = _persist_story(db, _story(), 0, 0, 0)

    assert ok is False
    assert (new_count, merged_count, evolved_count) == (0, 0, 0)
    db.add_story.assert_not_called()


def test_quality_skip_returns_ok_true_without_persisting():
    db = MagicMock()
    with patch("digest.daemon.assess_story_quality", return_value=_quality(True, "low_ai_relevance")):
        new_count, merged_count, evolved_count, ok = _persist_story(db, _story(), 0, 0, 0)

    assert ok is True
    assert (new_count, merged_count, evolved_count) == (0, 0, 0)
    db.add_story.assert_not_called()


def test_successful_new_story_returns_ok_true():
    db = MagicMock()
    db.find_similar.return_value = None
    db.add_story.return_value = "story-id-12345678"
    embed_response = MagicMock(embeddings=[[0.1, 0.2, 0.3]])

    with patch("digest.daemon.assess_story_quality", return_value=_quality()), \
         patch("digest.daemon.llm_client.embed", return_value=embed_response):
        new_count, merged_count, evolved_count, ok = _persist_story(db, _story(), 0, 0, 0)

    assert ok is True
    assert new_count == 1
    db.add_story.assert_called_once()


def test_successful_merge_returns_ok_true():
    db = MagicMock()
    db.find_similar.return_value = "existing-id"
    db.get_story.return_value = {"title": "Old title", "mention_count": 1, "is_read": False}
    embed_response = MagicMock(embeddings=[[0.1, 0.2, 0.3]])

    with patch("digest.daemon.assess_story_quality", return_value=_quality()), \
         patch("digest.daemon.llm_client.embed", return_value=embed_response):
        new_count, merged_count, evolved_count, ok = _persist_story(
            db, _story(), 0, 0, 0, skip_new_info_check=True
        )

    assert ok is True
    assert merged_count == 1
    db.add_mention.assert_called_once()
    db.add_story.assert_not_called()

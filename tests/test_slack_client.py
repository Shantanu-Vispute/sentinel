from unittest.mock import Mock, patch

import pytest
import requests

from digest.slack_client import SlackAPIError, SlackClient


def _response(payload, status=200, headers=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = payload
    return response


def test_post_message_uses_bearer_json_accessible_text_and_broadcast_thread():
    response = _response({"ok": True, "ts": "123.456"})
    client = SlackClient(token="xoxb-secret", api_base="https://slack.test/api")

    with patch("digest.slack_client.requests.post", return_value=response) as post:
        result = client.post_message(
            channel="C_ALL",
            text="Accessible fallback",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Story"}}],
            client_msg_id="stable-id",
            thread_ts="111.222",
        )

    assert result["ts"] == "123.456"
    _, kwargs = post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer xoxb-secret"
    assert kwargs["json"]["text"] == "Accessible fallback"
    assert kwargs["json"]["thread_ts"] == "111.222"
    assert kwargs["json"]["reply_broadcast"] is True
    assert kwargs["json"]["unfurl_links"] is True
    assert kwargs["json"]["unfurl_media"] is True
    assert kwargs["json"]["client_msg_id"] == "stable-id"


def test_rate_limit_preserves_retry_after():
    client = SlackClient(token="xoxb-secret")
    response = _response({}, status=429, headers={"Retry-After": "17"})

    with patch("digest.slack_client.requests.post", return_value=response), \
         pytest.raises(SlackAPIError) as caught:
        client.post_message(
            channel="C_ALL", text="text", blocks=[], client_msg_id="id"
        )

    assert caught.value.retryable is True
    assert caught.value.retry_after == 17
    assert caught.value.code == "ratelimited"


def test_permanent_api_error_is_not_retried():
    client = SlackClient(token="xoxb-secret")
    response = _response({"ok": False, "error": "invalid_auth"})

    with patch("digest.slack_client.requests.post", return_value=response), \
         pytest.raises(SlackAPIError) as caught:
        client.post_message(
            channel="C_ALL", text="text", blocks=[], client_msg_id="id"
        )

    assert caught.value.code == "invalid_auth"
    assert caught.value.retryable is False


def test_timeout_and_malformed_response_are_retryable():
    client = SlackClient(token="xoxb-secret")
    with patch(
        "digest.slack_client.requests.post",
        side_effect=requests.Timeout("slow"),
    ), pytest.raises(SlackAPIError) as timeout_error:
        client.post_message(
            channel="C_ALL", text="text", blocks=[], client_msg_id="id"
        )
    assert timeout_error.value.retryable is True

    malformed = _response({"ok": True})
    malformed.json.side_effect = ValueError("bad json")
    with patch("digest.slack_client.requests.post", return_value=malformed), \
         pytest.raises(SlackAPIError) as malformed_error:
        client.post_message(
            channel="C_ALL", text="text", blocks=[], client_msg_id="id"
        )
    assert malformed_error.value.code == "malformed_response"
    assert malformed_error.value.retryable is True


def test_get_permalink_uses_get_without_extra_scope_data():
    response = _response({"ok": True, "permalink": "https://workspace.slack.com/a"})
    client = SlackClient(token="xoxb-secret", api_base="https://slack.test/api")

    with patch("digest.slack_client.requests.get", return_value=response) as get:
        permalink = client.get_permalink(channel="C_ALL", message_ts="123.456")

    assert permalink == "https://workspace.slack.com/a"
    _, kwargs = get.call_args
    assert kwargs["params"] == {"channel": "C_ALL", "message_ts": "123.456"}

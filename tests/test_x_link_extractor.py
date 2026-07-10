from unittest.mock import MagicMock, patch

from digest.x_link_extractor import (
    canonical_tweet_url,
    find_x_links,
    find_x_links_in_page,
    fetch_page_html,
)


def test_canonical_tweet_url_matches_twitter_and_x():
    a = canonical_tweet_url("https://twitter.com/OpenAI/status/123")
    b = canonical_tweet_url("https://x.com/OpenAI/status/123?s=20")
    assert a == b == ("https://x.com/OpenAI/status/123", "123")


def test_canonical_tweet_url_matches_i_web_status():
    assert canonical_tweet_url("https://x.com/i/web/status/456") == (
        "https://x.com/i/web/status/456", "456",
    )


def test_canonical_tweet_url_rejects_non_status_links():
    assert canonical_tweet_url("https://x.com/OpenAI") is None
    assert canonical_tweet_url("https://example.com/status/123") is None
    assert canonical_tweet_url("") is None


def test_find_x_links_dedupes_by_tweet_id_across_domains():
    links = [
        {"href": "https://twitter.com/OpenAI/status/111"},
        {"href": "https://x.com/OpenAI/status/111?s=20"},
        "https://x.com/sama/status/222",
        {"href": "https://example.com/nope"},
    ]
    found = find_x_links(links)
    assert [l["tweet_id"] for l in found] == ["111", "222"]


def test_fetch_page_html_returns_none_on_failure():
    with patch("digest.x_link_extractor.requests.get", side_effect=RuntimeError("boom")):
        assert fetch_page_html("https://example.com/x") is None


def test_fetch_page_html_rejects_non_html_content_type():
    resp = MagicMock(status_code=200, text="{}")
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    with patch("digest.x_link_extractor.requests.get", return_value=resp):
        assert fetch_page_html("https://example.com/x.json") is None


def test_find_x_links_in_page_extracts_embedded_tweets():
    html = '<html><body><a href="https://x.com/sama/status/999">tweet</a></body></html>'
    resp = MagicMock(status_code=200, text=html)
    resp.headers = {"content-type": "text/html"}
    resp.raise_for_status = MagicMock()
    with patch("digest.x_link_extractor.requests.get", return_value=resp):
        found = find_x_links_in_page("https://blog.example.com/post")
    assert found == [{"url": "https://x.com/sama/status/999", "tweet_id": "999"}]

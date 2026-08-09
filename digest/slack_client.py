"""Small server-side client for the Slack Web API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from config import SLACK_API_BASE, SLACK_BOT_TOKEN


RETRYABLE_ERRORS = {
    "fatal_error",
    "internal_error",
    "ratelimited",
    "rate_limited",
    "request_timeout",
    "service_unavailable",
}


@dataclass
class SlackAPIError(RuntimeError):
    code: str
    retryable: bool = False
    retry_after: int | None = None

    def __str__(self) -> str:
        return self.code


class SlackClient:
    def __init__(
        self,
        token: str = SLACK_BOT_TOKEN,
        api_base: str = SLACK_API_BASE,
        timeout: int = 20,
    ):
        self.token = token.strip()
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _request(
        self, method: str, payload: dict[str, Any], *, http_method: str = "POST"
    ) -> dict[str, Any]:
        if not self.token:
            raise SlackAPIError("missing_token", retryable=False)
        url = f"{self.api_base}/{method}"
        try:
            if http_method == "GET":
                response = requests.get(
                    url,
                    params=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            else:
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise SlackAPIError(
                f"request_failed:{exc.__class__.__name__}", retryable=True
            ) from exc

        retry_after = None
        if response.status_code == 429:
            try:
                retry_after = max(1, int(response.headers.get("Retry-After", "1")))
            except (TypeError, ValueError):
                retry_after = 1
            raise SlackAPIError(
                "ratelimited", retryable=True, retry_after=retry_after
            )
        if response.status_code >= 500:
            raise SlackAPIError(
                f"http_{response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise SlackAPIError(
                f"http_{response.status_code}", retryable=False
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SlackAPIError("malformed_response", retryable=True) from exc
        if not isinstance(data, dict):
            raise SlackAPIError("malformed_response", retryable=True)
        if not data.get("ok"):
            code = str(data.get("error") or "unknown_error")
            raise SlackAPIError(
                code,
                retryable=code in RETRYABLE_ERRORS,
                retry_after=retry_after,
            )
        return data

    def post_message(
        self,
        *,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]],
        client_msg_id: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel,
            "text": text,
            "blocks": blocks,
            "client_msg_id": client_msg_id,
            "unfurl_links": True,
            "unfurl_media": True,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
            payload["reply_broadcast"] = True
        return self._request("chat.postMessage", payload)

    def get_permalink(self, *, channel: str, message_ts: str) -> str:
        data = self._request(
            "chat.getPermalink",
            {"channel": channel, "message_ts": message_ts},
            http_method="GET",
        )
        permalink = data.get("permalink")
        if not isinstance(permalink, str) or not permalink:
            raise SlackAPIError("missing_permalink", retryable=True)
        return permalink

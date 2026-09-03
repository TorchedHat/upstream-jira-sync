from __future__ import annotations

import logging
import time
from typing import Any, Final
from urllib.parse import urlsplit

import requests

from upstream_jira_sync.models import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT

log = logging.getLogger(__name__)


class RetryExhaustedError(RuntimeError):
    """Raised when every rate-limit retry for one request came back 429."""


class BaseHTTPClient:
    """Shared HTTP client with rate-limit retry and default timeouts."""

    _MAX_RETRIES: Final[int] = 4
    _BASE_DELAY: Final[int] = 2

    def __init__(self) -> None:
        self._session = requests.Session()

    def __enter__(self) -> BaseHTTPClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self._session.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Make an HTTP request with automatic retry on rate-limit responses."""
        kwargs.setdefault("timeout", (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT))

        delay = self._BASE_DELAY

        for attempt in range(self._MAX_RETRIES):
            response = self._session.request(method, url, **kwargs)

            is_rate_limited = response.status_code == 429 or (
                response.status_code == 403 and "Retry-After" in response.headers
            )

            if is_rate_limited:
                wait = _parse_retry_after(response.headers.get("Retry-After"), delay)
                log.warning(
                    "Rate limited (%d) on %s — waiting %ds (attempt %d/%d)",
                    response.status_code,
                    url,
                    wait,
                    attempt + 1,
                    self._MAX_RETRIES,
                )
                time.sleep(wait)
                delay *= 2
                continue

            if 400 <= response.status_code < 600:
                # raise_for_status embeds response.url in the message; query
                # params can carry roster emails (JQL, user search), so raise
                # with the query stripped instead (R10).
                raise requests.HTTPError(
                    f"{response.status_code} error for url: "
                    f"{_without_query(response.url)}",
                    response=response,
                )
            return response

        raise RetryExhaustedError(
            f"Exceeded {self._MAX_RETRIES} retries for {method} {url}"
        )


def _without_query(url: str) -> str:
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _parse_retry_after(header_value: str | None, default: int) -> int:
    """Safely parse a Retry-After header value. Clamps to non-negative."""
    if header_value is None:
        return default
    try:
        return max(0, int(header_value))
    except ValueError:
        return default

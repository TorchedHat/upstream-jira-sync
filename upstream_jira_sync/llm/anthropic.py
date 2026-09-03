from __future__ import annotations

import logging
import os
from typing import Final

import requests

from upstream_jira_sync.config import LLMSettings
from upstream_jira_sync.llm.base import (
    LLMFatalError,
    MessagesProvider,
    cacheable_system,
)

log = logging.getLogger(__name__)

_API_BASE: Final[str] = "https://api.anthropic.com"
_API_VERSION: Final[str] = "2023-06-01"


class AnthropicProvider(MessagesProvider):
    """Calls the model via the Anthropic Messages API using ``ANTHROPIC_API_KEY``."""

    label = "Anthropic API"

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__(settings)
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key and not settings.base_url:
            raise ValueError("Environment variable ANTHROPIC_API_KEY is not set")
        base = settings.base_url.rstrip("/") if settings.base_url else _API_BASE
        self._url = f"{base}/v1/messages"
        self._models_url = f"{base}/v1/models"
        self._session.headers.update(
            {
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": _API_VERSION,
            }
        )

    def _diagnose(
        self, status: int | None, kind: str, message: str
    ) -> tuple[bool, str]:
        if status == 404 or kind == "not_found_error":
            return True, f"check llm.model={self._model!r}"
        if status in (401, 403) or kind in ("authentication_error", "permission_error"):
            return True, "check the ANTHROPIC_API_KEY secret"
        # Out of credit is reported as a 400 invalid_request_error.
        if status == 400 and "credit balance" in message.lower():
            return True, "add credit or raise the spend limit in the Anthropic Console"
        return False, ""

    def preflight(self) -> None:
        """Check the API key and model id with ``GET /v1/models/{model}``.

        A bad key, missing permission or unknown model raises LLMFatalError.
        Any other failure (network, 5xx, endpoint absent) is logged and
        ignored; the first real call reports it."""
        try:
            self._request("GET", f"{self._models_url}/{self._model}")
        except requests.HTTPError as exc:
            err = self._classify(exc)
            if isinstance(err, LLMFatalError):
                raise err from exc
            log.warning("LLM preflight inconclusive (%s); continuing.", err)
        except requests.RequestException as exc:
            log.warning("LLM preflight inconclusive (%s); continuing.", exc)
        else:
            log.info("LLM preflight OK: model %s reachable", self._model)

    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        return self._complete(
            self._url,
            {
                "model": self._model,
                "max_tokens": max_tokens,
                "system": cacheable_system(system),
                "messages": [{"role": "user", "content": user_message}],
            },
        )

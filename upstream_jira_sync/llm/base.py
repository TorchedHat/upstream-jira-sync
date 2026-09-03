from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from typing import Final, Protocol, runtime_checkable

import requests

from upstream_jira_sync.config import LLMSettings
from upstream_jira_sync.http import BaseHTTPClient, RetryExhaustedError


class LLMError(RuntimeError):
    """An LLM call failed; the message carries the provider's own error text."""


class LLMFatalError(LLMError):
    """The provider cannot serve this run: bad key, permission denied, unknown
    model or exhausted credit. AI classes re-raise it and the CLI exits 1."""


# Statuses that mean the LLM is misconfigured for the whole run.
FATAL_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 404})


def api_error_detail(exc: requests.HTTPError) -> tuple[int | None, str, str]:
    """(status, error kind, error message) from a Messages-API error response.

    Handles both Anthropic's ``{"error": {"type", "message"}}`` and Google's
    ``{"error": {"code", "status", "message"}}`` (Vertex auth/quota/policy
    errors). Falls back to the raw body / exception text when the body is
    not JSON."""
    resp = exc.response
    if resp is None:
        return None, "", str(exc)
    try:
        err = resp.json().get("error") or {}
        kind = str(err.get("status") or err.get("type") or "")
        return resp.status_code, kind, str(err.get("message", ""))
    except (ValueError, AttributeError):
        return resp.status_code, "", (resp.text or str(exc)).strip()[:500]


def cacheable_system(text: str) -> list[dict[str, object]]:
    """The system prompt as a single cacheable content block.

    The prompt is identical for every call in a run, so the API can reuse it.
    Blocks under the model's caching minimum are sent uncached."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


class MessagesProvider(BaseHTTPClient):
    """Shared transport for providers that speak the Anthropic Messages wire
    format (api.anthropic.com and Vertex ``rawPredict``).

    Subclasses set ``label`` (the prefix on every error message), build the
    request in ``complete`` and hand it to ``_complete``, and override
    ``_diagnose`` to add provider-specific fatal rules and hints."""

    label: str = "LLM"

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__()
        self._model = settings.model

    def _diagnose(
        self, status: int | None, kind: str, message: str
    ) -> tuple[bool, str]:
        """(fatal, hint) for provider-specific cases. 401/403/404 are already
        fatal in ``_classify``; return True here to make other responses fatal
        (e.g. a 400 that means "out of credit"). The hint is appended to the
        error text in parentheses when non-empty."""
        return False, ""

    def _classify(self, exc: requests.HTTPError) -> LLMError:
        """Turn an HTTP error into LLMError (skip this call) or LLMFatalError
        (abort the run), carrying the provider's own message."""
        status, kind, message = api_error_detail(exc)
        detail = f"{self.label} {status or 'error'}"
        if kind:
            detail += f" {kind}"
        detail += f": {message or 'no error message in response'}"

        extra_fatal, hint = self._diagnose(status, kind, message)
        if hint:
            detail += f" ({hint})"
        fatal = status in FATAL_STATUSES or extra_fatal
        return LLMFatalError(detail) if fatal else LLMError(detail)

    def _complete(self, url: str, body: dict[str, object]) -> str:
        """POST a Messages request and return the first content block's text,
        mapping every transport / API failure onto the LLMError hierarchy."""
        try:
            resp = self._request("POST", url, json=body)
        except requests.HTTPError as exc:
            raise self._classify(exc) from exc
        except requests.RequestException as exc:
            raise LLMError(f"{self.label} request failed: {exc}") from exc
        except RetryExhaustedError as exc:
            raise LLMError(
                f"{self.label} rate limit not clearing (quota exhausted?): {exc}"
            ) from exc
        try:
            return resp.json()["content"][0]["text"].strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.label} returned an unexpected body: {exc}") from exc


@runtime_checkable
class LLMProvider(Protocol):
    """Single-turn completion surface consumed by every AI class (R9).

    The model is bound at provider construction from LLMSettings.model, so
    callers pass only prompt content. Implementations must return the
    stripped text of the first content block and raise on transport errors
    (AI classes catch and skip)."""

    def complete(
        self,
        system: str,
        user_message: str,
        max_tokens: int = 256,
    ) -> str: ...


_BUILTIN_PROVIDERS: Final[dict[str, str]] = {
    "vertex": "upstream_jira_sync.llm.vertex:VertexProvider",
    "anthropic": "upstream_jira_sync.llm.anthropic:AnthropicProvider",
}


def load_provider(settings: LLMSettings) -> LLMProvider:
    """Resolve settings.provider via the 'upstream_jira_sync.llm' entry-point group,
    falling back to the built-in map. Entry points load a class (or factory)
    called as factory(settings) -> LLMProvider."""
    for ep in entry_points(group="upstream_jira_sync.llm"):
        if ep.name == settings.provider:
            return ep.load()(settings)

    target = _BUILTIN_PROVIDERS.get(settings.provider)
    if target is None:
        known = sorted(
            set(_BUILTIN_PROVIDERS)
            | {ep.name for ep in entry_points(group="upstream_jira_sync.llm")}
        )
        raise ValueError(
            f"Unknown llm.provider {settings.provider!r}; available: {known}"
        )
    module_name, _, attr = target.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory(settings)


def provider_load_error(provider: str) -> str:
    """Import (without constructing) the configured provider. Empty string when
    loadable, else the error — e.g. a missing optional extra like [vertex]."""
    try:
        for ep in entry_points(group="upstream_jira_sync.llm"):
            if ep.name == provider:
                ep.load()
                return ""
        target = _BUILTIN_PROVIDERS.get(provider)
        if target is None:
            return f"unknown llm.provider {provider!r}"
        module_name, _, attr = target.partition(":")
        getattr(importlib.import_module(module_name), attr)
        return ""
    except ImportError as exc:
        return str(exc)

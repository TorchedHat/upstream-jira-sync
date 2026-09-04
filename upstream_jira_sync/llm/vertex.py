from __future__ import annotations

import logging
from typing import Final

from upstream_jira_sync.config import LLMSettings
from upstream_jira_sync.llm.base import LLMFatalError, MessagesProvider

try:
    import google.auth
    from google.auth import exceptions as google_auth_exceptions
    from google.auth.transport.requests import Request as AuthRequest
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Vertex AI provider requires google-auth. "
        "Install with: pip install upstream-jira-sync[vertex]"
    ) from e

log = logging.getLogger(__name__)

_API_VERSION: Final[str] = "vertex-2023-10-16"
# Service-account credentials have no default scope; the token refresh is
# rejected with "invalid_scope" without one.
_SCOPES: Final[tuple[str, ...]] = ("https://www.googleapis.com/auth/cloud-platform",)


def _vertex_host(region: str) -> str:
    """The ``global`` region has no regional hostname; every other region
    is served from ``{region}-aiplatform.googleapis.com``."""
    if region == "global":
        return "https://aiplatform.googleapis.com"
    return f"https://{region}-aiplatform.googleapis.com"


class VertexProvider(MessagesProvider):
    """Calls the model through Vertex AI's Anthropic ``rawPredict`` endpoint.

    Auth is Google Application Default Credentials, normally a service-account
    key file pointed to by ``GOOGLE_APPLICATION_CREDENTIALS``."""

    label = "Vertex AI"

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__(settings)
        self._project = settings.vertex_project
        self._region = settings.vertex_region
        project, region = self._project, self._region
        self._session.headers.update({"content-type": "application/json"})
        path = f"/v1/projects/{project}/locations/{region}/publishers/anthropic/models/"
        if settings.base_url:
            # Mock server; skip GCP auth entirely.
            self._url_prefix = f"{settings.base_url.rstrip('/')}{path}"
            self._credentials = None
        else:
            self._url_prefix = f"{_vertex_host(region)}{path}"
            self._refresh_token()

    def _diagnose(
        self, status: int | None, kind: str, message: str
    ) -> tuple[bool, str]:
        if status == 404:
            return True, (
                f"check llm.model={self._model!r} and llm.vertex_region={self._region!r}"
            )
        if status in (401, 403):
            return True, (
                f"check the GCP_CREDENTIALS secret and its access to project {self._project!r}"
            )
        # Org-policy denials and unknown models on the global endpoint are 400s.
        lowered = message.lower()
        if status == 400 and (
            "organization policy" in lowered or "not found" in lowered
        ):
            return True, (
                f"model {self._model!r} is not available in project "
                f"{self._project!r}/{self._region!r}"
            )
        return False, ""

    def _refresh_token(self) -> None:
        try:
            self._credentials, _ = google.auth.default(scopes=list(_SCOPES))
            self._credentials.refresh(AuthRequest())
        except google_auth_exceptions.GoogleAuthError as exc:
            raise LLMFatalError(
                f"Vertex AI credentials unavailable: {exc} "
                "(check GOOGLE_APPLICATION_CREDENTIALS / the GCP_CREDENTIALS secret)"
            ) from exc
        self._session.headers["Authorization"] = f"Bearer {self._credentials.token}"

    def _ensure_valid_token(self) -> None:
        if self._credentials and not self._credentials.valid:
            self._refresh_token()

    def preflight(self) -> None:
        """Refresh the access token before any Jira write.

        Raises LLMFatalError when the credentials are unusable. Vertex has no
        free model-lookup endpoint, so model and quota problems surface on the
        first real call."""
        if self._credentials is None:
            log.info("LLM preflight skipped: mock base_url in use")
            return
        self._refresh_token()
        log.info(
            "LLM preflight OK: Vertex credentials valid for project %s (%s/%s)",
            self._project,
            self._region,
            self._model,
        )

    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        self._ensure_valid_token()
        # Vertex names the model in the URL, not the body.
        body = self._request_body(system, user_message, max_tokens)
        del body["model"]
        body["anthropic_version"] = _API_VERSION
        return self._complete(f"{self._url_prefix}{self._model}:rawPredict", body)

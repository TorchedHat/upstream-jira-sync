"""Jira-based bot control: enable/disable and sync frequency override."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from upstream_jira_sync.config import AppConfig
    from upstream_jira_sync.jira import JiraClient

log = logging.getLogger(__name__)


class BotControl:
    """Query Jira for bot control settings (enabled/disabled, sync frequency).

    Fail-loud behavior: all errors abort immediately with clear messages.
    No graceful fallback or defaults if Jira queries fail.
    """

    # Valid values for bot control flag field
    VALID_FLAGS = frozenset({"enabled", "disabled", "suspended"})

    def __init__(self, jira_client: JiraClient, config: AppConfig) -> None:
        self.jira = jira_client
        self.config = config

    def get_control_state(self) -> dict:
        """Query Jira for bot control settings.

        Returns:
            {
                "enabled": bool,
                "sync_interval_hours": int | None,
                "control_issue_key": str | None,
            }

        Raises:
            RuntimeError: If bot_control_enabled=true and query/validation fails.
        """
        # Feature disabled in config: return default enabled state
        if not self.config.bot_control_enabled:
            return {
                "enabled": True,
                "sync_interval_hours": None,
                "control_issue_key": None,
            }

        # Feature enabled: must succeed, no fallback
        control_issue_key = self.config.bot_control_issue
        enabled_field = self.config.bot_control_flag_field
        interval_field = self.config.bot_control_interval_field

        if not control_issue_key:
            raise RuntimeError(
                "bot_control_enabled=true but bot_control_issue not configured"
            )

        try:
            issue = self.jira.get_issue(control_issue_key)
        except Exception as e:
            raise RuntimeError(
                f"Failed to query bot control issue {control_issue_key}: {e}"
            ) from e

        if not issue:
            raise RuntimeError(
                f"Bot control issue {control_issue_key} not found in Jira. "
                f"Check bot_control_issue setting."
            )

        fields = issue.get("fields", {})

        # Validate and extract enabled flag
        enabled_value = fields.get(enabled_field)
        if not enabled_value:
            raise RuntimeError(
                f"Bot control issue {control_issue_key} missing field {enabled_field}. "
                f"Check bot_control_flag_field setting."
            )

        if enabled_value not in self.VALID_FLAGS:
            raise RuntimeError(
                f"Bot control flag has invalid value: {enabled_value!r}. "
                f"Must be one of: {', '.join(sorted(self.VALID_FLAGS))}"
            )

        enabled = enabled_value == "enabled"

        # Extract and validate sync interval (optional)
        interval_hours = None
        if interval_field:
            interval_value = fields.get(interval_field)
            if interval_value is not None:
                try:
                    interval_hours = int(interval_value)
                except (ValueError, TypeError):
                    raise RuntimeError(
                        f"Bot control sync_interval_hours has invalid value: "
                        f"{interval_value!r}. Must be a positive integer."
                    ) from None

                if interval_hours <= 0:
                    raise RuntimeError(
                        f"Bot control sync_interval_hours must be > 0, got {interval_hours}"
                    )

        log.info(
            f"Bot control state: enabled={enabled}, "
            f"sync_interval_hours={interval_hours}, "
            f"issue={control_issue_key}"
        )

        return {
            "enabled": enabled,
            "sync_interval_hours": interval_hours,
            "control_issue_key": control_issue_key,
        }

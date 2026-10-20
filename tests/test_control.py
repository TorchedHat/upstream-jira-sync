"""Tests for Jira-based bot control feature."""

from __future__ import annotations

import pytest

from upstream_jira_sync.control import BotControl
from conftest import make_config


class FakeJiraClient:
    """Mock JiraClient for testing BotControl."""

    def __init__(self, issue_data: dict | None = None, raises: Exception | None = None):
        self.issue_data = issue_data
        self.raises = raises
        self.queries: list[str] = []

    def get_issue(self, issue_key: str) -> dict | None:
        self.queries.append(issue_key)
        if self.raises:
            raise self.raises
        return self.issue_data


def test_bot_control_disabled_in_config():
    """Feature is no-op when bot_control_enabled=false."""
    config = make_config(bot_control_enabled=False)
    jira = FakeJiraClient()

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state == {
        "enabled": True,
        "sync_interval_hours": None,
        "control_issue_key": None,
    }
    assert jira.queries == []  # Never queried


def test_bot_control_enabled_flag():
    """Bot control flag='enabled' returns enabled=True."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {"customfield_10042": "enabled"},
        }
    )

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state["enabled"] is True
    assert state["sync_interval_hours"] is None
    assert state["control_issue_key"] == "OPS-1"


def test_bot_control_disabled_flag():
    """Bot control flag='disabled' returns enabled=False."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {"customfield_10042": "disabled"},
        }
    )

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state["enabled"] is False


def test_bot_control_suspended_flag():
    """Bot control flag='suspended' returns enabled=False."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {"customfield_10042": "suspended"},
        }
    )

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state["enabled"] is False


def test_bot_control_interval_override():
    """Sync interval from Jira overrides config value."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
        bot_control_interval_field="customfield_10043",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {
                "customfield_10042": "enabled",
                "customfield_10043": 24,
            },
        }
    )

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state["sync_interval_hours"] == 24


def test_bot_control_interval_as_string():
    """Sync interval can be provided as string number (coerced to int)."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
        bot_control_interval_field="customfield_10043",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {
                "customfield_10042": "enabled",
                "customfield_10043": "48",  # string instead of int
            },
        }
    )

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state["sync_interval_hours"] == 48


def test_bot_control_issue_missing_aborts():
    """Aborts (doesn't fallback) if control issue not found."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(issue_data=None)

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="Bot control issue OPS-1 not found"):
        control.get_control_state()


def test_bot_control_query_fails_aborts():
    """Aborts if Jira query raises exception."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(raises=ConnectionError("Jira timeout"))

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="Failed to query bot control issue"):
        control.get_control_state()


def test_bot_control_missing_flag_field_aborts():
    """Aborts if control issue missing flag field."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {},  # Missing flag field
        }
    )

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="missing field customfield_10042"):
        control.get_control_state()


def test_bot_control_invalid_flag_value_aborts():
    """Aborts if flag has invalid value."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {"customfield_10042": "invalid"},
        }
    )

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="invalid value"):
        control.get_control_state()


def test_bot_control_invalid_interval_aborts():
    """Aborts if sync_interval_hours is invalid (non-numeric)."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
        bot_control_interval_field="customfield_10043",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {
                "customfield_10042": "enabled",
                "customfield_10043": "not-a-number",
            },
        }
    )

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="invalid value"):
        control.get_control_state()


def test_bot_control_interval_zero_aborts():
    """Aborts if sync_interval_hours is <= 0."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
        bot_control_interval_field="customfield_10043",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {
                "customfield_10042": "enabled",
                "customfield_10043": 0,
            },
        }
    )

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="must be > 0"):
        control.get_control_state()


def test_bot_control_interval_negative_aborts():
    """Aborts if sync_interval_hours is negative."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
        bot_control_interval_field="customfield_10043",
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {
                "customfield_10042": "enabled",
                "customfield_10043": -5,
            },
        }
    )

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="must be > 0"):
        control.get_control_state()


def test_bot_control_no_config_issue_aborts():
    """Aborts if bot_control_enabled=true but issue not configured."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="",  # Missing
        bot_control_flag_field="customfield_10042",
    )
    jira = FakeJiraClient()

    control = BotControl(jira_client=jira, config=config)

    with pytest.raises(RuntimeError, match="bot_control_issue not configured"):
        control.get_control_state()


def test_bot_control_interval_optional():
    """Sync interval is optional (can be None if interval_field not configured)."""
    config = make_config(
        bot_control_enabled=True,
        bot_control_issue="OPS-1",
        bot_control_flag_field="customfield_10042",
        bot_control_interval_field="",  # Not configured
    )
    jira = FakeJiraClient(
        issue_data={
            "key": "OPS-1",
            "fields": {"customfield_10042": "enabled"},
        }
    )

    control = BotControl(jira_client=jira, config=config)
    state = control.get_control_state()

    assert state["sync_interval_hours"] is None

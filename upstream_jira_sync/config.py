from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

import yaml

from upstream_jira_sync.models import CanonicalStatus, TeamMember

MODES: Final[tuple[str, str]] = ("shadow", "auto")

DEFAULT_STATUS_MAP: Final[dict[str, str]] = {
    "todo": "To Do",
    "in_progress": "In Progress",
    "review": "In Review",
    "done": "Done",
}

DEFAULT_BOT_LOGINS: Final[tuple[str, ...]] = (
    "dependabot[bot]",
    "github-actions[bot]",
)

_CUSTOM_FIELD_RE = re.compile(r"^customfield_\d+$")
_MANUAL_OVERRIDE_FIELD_RE = re.compile(
    r"^(status|summary|description|labels|assignee|customfield_\d+)$"
)


@dataclass
class TeamSpec:
    """One entry of the config-driven team taxonomy (R7)."""

    name: str
    label: str
    team_id: str = ""  # optional Atlassian team UUID for the native Team field


@dataclass
class LLMSettings:
    """Pluggable LLM provider selection (R9). base_url is set by the CLI for
    --mock-url routing only, never from config.yaml."""

    provider: str = "anthropic"
    model: str = ""
    vertex_project: str = ""
    vertex_region: str = "us-east5"
    base_url: str = ""


@dataclass
class AppConfig:
    """API credentials, roster, and all sync behavior settings.

    Single source of truth: every module receives values through this object.
    Secrets come from env vars; the roster comes from ROSTER_YAML (env) or
    roster_file (R10). No tenant-specific defaults anywhere (R3).
    """

    # Roster
    team: list[TeamMember]

    # GitHub
    github_token: str
    github_repo: list[str]

    # Jira
    jira_url: str
    jira_email: str
    jira_token: str
    jira_project_key: str
    jira_account_id: str = ""
    jira_bot_aliases: list[str] = field(default_factory=list)
    jira_components: list[str] = field(default_factory=list)

    # Workflow (R4, R5, R6)
    status_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_STATUS_MAP))
    issue_type: str = "Story"
    container_issue_type: str = "Epic"
    merge_labels: list[str] = field(default_factory=list)

    # LLM (R9)
    llm: LLMSettings = field(default_factory=LLMSettings)

    # Tuning
    poll_interval_hours: int = 25
    significant_comments_threshold: int = 3
    ignore_pr_labels: list[str] = field(default_factory=list)
    # Activity by these GitHub logins (plus any App-typed or [bot]-suffixed
    # account) never counts as PR activity: it can't pull a PR back into the
    # sync window, reopen a closed ticket, or reset the staleness clock.
    ignore_activity_authors: list[str] = field(default_factory=list)
    bot_logins: list[str] = field(default_factory=lambda: list(DEFAULT_BOT_LOGINS))
    # Jira labels that opt an issue out of bot-driven metadata writes: the
    # sprint sweep and the team label / Team field backfill both skip it.
    # Self-service escape hatch for human-owned cards (standing status-report
    # issues, planning placeholders) that live in an active status forever and
    # would otherwise be re-added to every sprint. Matched case-insensitively.
    automation_opt_out_labels: list[str] = field(default_factory=list)

    # Story point estimation (field required when enabled, R8)
    enable_estimation: bool = False
    story_points_field: str = ""
    # Multi-user picker custom field collecting co-authors on multi-author PRs.
    # Empty disables (co-authors still get the Jira note + digest event).
    contributors_field: str = ""

    # Prompt overrides: directory of per-file overrides for packaged skills (R13)
    skills_dir: str = ""

    # Issue claiming / auto-create
    enable_auto_create: bool = False
    claim_mode: str = "shadow"

    # Stale-PR close sweep (0 days disables)
    stale_pr_close_days: int = 21
    stale_pr_close_mode: str = "shadow"

    # Sprints (sprint_field/board required when any sprint feature enabled, R8)
    enable_sprint_tagging: bool = False
    sprint_mode: str = "shadow"
    sprint_anchor_date: str = ""
    sprint_anchor_number: int = 0
    sprint_length_days: int = 14
    enable_sprint_sweep: bool = False
    sprint_sweep_mode: str = "shadow"
    sprint_sweep_lookback_sprints: int = 1
    enable_sprint_provision: bool = False
    sprint_provision_mode: str = "shadow"
    sprint_provision_lookahead: int = 2
    sprint_board_id: int = 0
    sprint_name_format: str = "Sprint {number}"
    sprint_field: str = ""

    # Weekly digest — posted as a GitHub Discussion
    digest_enabled: bool = False
    digest_include_manual: bool = True
    digest_include_reviews: bool = True
    digest_repo: str = ""
    digest_category_slug: str = "announcements"
    digest_title_prefix: str = "Weekly digest"

    # Manual override persistence
    enable_manual_override: bool = True
    manual_override_mode: str = "shadow"
    manual_override_fields: list[str] = field(default_factory=lambda: ["status"])

    # Team assignment (taxonomy from config, R7; team_field gated, R8)
    enable_team_assignment: bool = False
    team_assignment_mode: str = "shadow"
    teams: list[TeamSpec] = field(default_factory=list)
    team_field: str = ""

    # RFC/container-issue tracking (epic_name_field optional; omitted from the
    # create payload when unset, R8)
    enable_rfc_epics: bool = False
    rfc_epic_mode: str = "shadow"
    rfc_overrides: dict = field(default_factory=dict)
    epic_name_field: str = ""

    # Low-confidence email pings
    enable_low_conf_email: bool = False
    low_conf_email_mode: str = "shadow"
    low_conf_email_from: str = ""

    _REQUIRED_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("github_repo", "settings.github_repo is missing or empty"),
        ("jira_url", "settings.jira_url is missing"),
        ("jira_project_key", "settings.jira_project_key is missing"),
    ]

    _REQUIRED_RUNTIME_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("team", "roster is empty: set ROSTER_YAML or provide roster_file"),
        ("github_token", "Environment variable GITHUB_TOKEN (or GH_PAT) is not set"),
        ("jira_email", "Environment variable JIRA_EMAIL is not set"),
        ("jira_token", "Environment variable JIRA_API_TOKEN is not set"),
    ]

    _MODE_ATTRS: ClassVar[list[str]] = [
        "claim_mode",
        "stale_pr_close_mode",
        "sprint_mode",
        "sprint_sweep_mode",
        "sprint_provision_mode",
        "manual_override_mode",
        "team_assignment_mode",
        "rfc_epic_mode",
        "low_conf_email_mode",
    ]

    # -- Derived accessors -----------------------------------------------------

    def status_name(self, status: CanonicalStatus) -> str:
        """Instance status name for a canonical state (R4)."""
        return self.status_map[status.value]

    @property
    def active_status_names(self) -> list[str]:
        """Status names counted as 'open work' in JQL filters."""
        return [
            self.status_map[s.value]
            for s in (
                CanonicalStatus.TODO,
                CanonicalStatus.IN_PROGRESS,
                CanonicalStatus.REVIEW,
            )
        ]

    @property
    def sprint_features_enabled(self) -> bool:
        return (
            self.enable_sprint_tagging
            or self.enable_sprint_sweep
            or self.enable_sprint_provision
        )

    # -- Validation --------------------------------------------------------------

    def validate(
        self,
        *,
        require_runtime: bool = True,
        extra_errors: list[str] | None = None,
    ) -> None:
        """Raise ValueError listing every configuration problem at once.

        require_runtime=False skips secrets/roster presence (offline check-config);
        everything else is still validated."""
        required = list(self._REQUIRED_FIELDS)
        if require_runtime:
            required += self._REQUIRED_RUNTIME_FIELDS
        errors = list(extra_errors or [])
        errors += [msg for attr, msg in required if not getattr(self, attr)]
        errors += self._validate_status_map()
        errors += self._validate_modes()
        errors += self._validate_llm()
        errors += self._validate_custom_fields()
        errors += self._validate_sprints()
        errors += self._validate_teams()
        errors += self._validate_misc()
        if errors:
            raise ValueError(
                "Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
            )

    def _validate_status_map(self) -> list[str]:
        canonical = {s.value for s in CanonicalStatus}
        provided = set(self.status_map)
        errors = []
        if missing := canonical - provided:
            errors.append(f"status_map is missing states: {sorted(missing)}")
        if unknown := provided - canonical:
            errors.append(
                f"status_map has unknown states {sorted(unknown)}; "
                f"allowed: {sorted(canonical)}"
            )
        for state, name in self.status_map.items():
            if not isinstance(name, str) or not name.strip():
                errors.append(f"status_map.{state} must be a non-empty status name")
        return errors

    def _validate_modes(self) -> list[str]:
        # Validated even for disabled features: a bad mode value would silently
        # no-op until the feature flag flips on.
        return [
            f"{mode_attr} must be 'shadow' or 'auto', got {getattr(self, mode_attr)!r}"
            for mode_attr in self._MODE_ATTRS
            if getattr(self, mode_attr) not in MODES
        ]

    def _validate_llm(self) -> list[str]:
        errors = []
        if not self.llm.provider:
            errors.append("llm.provider is required")
        if not self.llm.model:
            errors.append("llm.model is required")
        if self.llm.provider == "vertex" and not self.llm.vertex_project:
            errors.append("llm.vertex_project is required when llm.provider=vertex")
        return errors

    def _validate_custom_fields(self) -> list[str]:
        """Custom field ids are required only when their feature is enabled (R8)."""
        errors = []
        checks = [
            ("story_points_field", self.story_points_field, self.enable_estimation),
            ("sprint_field", self.sprint_field, self.sprint_features_enabled),
            (
                "team_field",
                self.team_field,
                self.enable_team_assignment and any(t.team_id for t in self.teams),
            ),
            ("epic_name_field", self.epic_name_field, False),
        ]
        for name, value, required in checks:
            if required and not value:
                errors.append(f"settings.{name} is required for the enabled feature")
            if value and not _CUSTOM_FIELD_RE.match(value):
                errors.append(
                    f"settings.{name} must look like customfield_12345, got {value!r}"
                )
        return errors

    def _validate_sprints(self) -> list[str]:
        if not self.sprint_features_enabled:
            return []
        errors = []
        if self.sprint_board_id <= 0:
            errors.append("sprint_board_id is required when sprint features are on")
        if not self.sprint_anchor_date:
            errors.append("sprint_anchor_date is required when sprint features are on")
        if self.sprint_anchor_number <= 0:
            errors.append(
                "sprint_anchor_number is required when sprint features are on"
            )
        if "{number}" not in self.sprint_name_format:
            errors.append("sprint_name_format must contain '{number}'")
        if self.sprint_length_days <= 0:
            errors.append("sprint_length_days must be positive")
        return errors

    def _validate_teams(self) -> list[str]:
        errors = []
        if self.enable_team_assignment and not self.teams:
            errors.append(
                "settings.teams must list at least one team when "
                "enable_team_assignment is on (R7)"
            )
        names = [t.name for t in self.teams]
        labels = [t.label for t in self.teams]
        if len(set(names)) != len(names):
            errors.append("settings.teams names must be unique")
        if len(set(labels)) != len(labels):
            errors.append("settings.teams labels must be unique")
        for t in self.teams:
            if not t.name or not t.label:
                errors.append("every settings.teams entry needs both name and label")
        return errors

    def _validate_misc(self) -> list[str]:
        errors = []
        if not (1 <= self.poll_interval_hours <= 720):
            errors.append(
                f"poll_interval_hours must be between 1 and 720, "
                f"got {self.poll_interval_hours}"
            )
        for f_id in self.manual_override_fields:
            if not _MANUAL_OVERRIDE_FIELD_RE.match(f_id):
                errors.append(
                    f"manual_override_fields entry {f_id!r} is not a known field id"
                )
        for member in self.team:
            if not member.github or not member.jira_email:
                errors.append("every roster entry needs both github and jira_email")
        if self.skills_dir and not os.path.isdir(self.skills_dir):
            errors.append(f"settings.skills_dir does not exist: {self.skills_dir}")
        if self.enable_low_conf_email and self.low_conf_email_mode == "auto":
            if not self.low_conf_email_from:
                errors.append("low_conf_email_from is required in auto mode")
        if self.digest_enabled and not self.digest_repo:
            errors.append("digest_repo is required when digest_enabled is on")
        return errors

    # -- Loading -----------------------------------------------------------------

    @classmethod
    def load(
        cls, path: str = "config.yaml", *, require_runtime: bool = True
    ) -> AppConfig:
        """Load config.yaml, roster (ROSTER_YAML env or roster_file), and env
        secrets; validate everything and return the config."""
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        settings = raw.get("settings", {}) or {}

        unknown_key_errors = _unknown_key_errors(
            settings, _KNOWN_SETTINGS_KEYS, "settings"
        )
        unknown_key_errors += _unknown_key_errors(
            settings.get("llm") or {}, _KNOWN_LLM_KEYS, "settings.llm"
        )

        config_dir = os.path.dirname(os.path.abspath(path))
        team = _load_roster(config_dir, settings.get("roster_file", "team_roster.yaml"))

        skills_dir = settings.get("skills_dir", "")
        if skills_dir and not os.path.isabs(skills_dir):
            skills_dir = os.path.join(config_dir, skills_dir)

        repos = settings.get("github_repo") or []
        if isinstance(repos, str):
            repos = [repos]

        status_map = dict(DEFAULT_STATUS_MAP)
        status_map.update(settings.get("status_map") or {})

        config = cls(
            team=team,
            github_token=os.environ.get("GITHUB_TOKEN", "")
            or os.environ.get("GH_PAT", ""),
            github_repo=repos,
            jira_url=settings.get("jira_url", ""),
            jira_email=os.environ.get("JIRA_EMAIL", ""),
            jira_token=os.environ.get("JIRA_API_TOKEN", ""),
            jira_project_key=settings.get("jira_project_key", ""),
            jira_account_id=os.environ.get("JIRA_ACCOUNT_ID", ""),
            jira_bot_aliases=settings.get("jira_bot_aliases", []) or [],
            jira_components=settings.get("jira_components", []) or [],
            status_map=status_map,
            issue_type=settings.get("issue_type", "Story"),
            container_issue_type=settings.get("container_issue_type", "Epic"),
            merge_labels=settings.get("merge_labels", []) or [],
            llm=_load_llm(settings.get("llm") or {}),
            poll_interval_hours=settings.get("poll_interval_hours", 25),
            significant_comments_threshold=settings.get(
                "significant_comments_threshold", 3
            ),
            ignore_pr_labels=settings.get("ignore_pr_labels") or [],
            ignore_activity_authors=settings.get("ignore_activity_authors") or [],
            bot_logins=settings.get("bot_logins", list(DEFAULT_BOT_LOGINS)) or [],
            automation_opt_out_labels=settings.get("automation_opt_out_labels") or [],
            enable_estimation=settings.get("enable_estimation", False),
            story_points_field=settings.get("story_points_field", ""),
            contributors_field=settings.get("contributors_field", ""),
            skills_dir=skills_dir,
            enable_auto_create=settings.get("enable_auto_create", False),
            claim_mode=settings.get("claim_mode", "shadow"),
            stale_pr_close_days=settings.get("stale_pr_close_days", 21),
            stale_pr_close_mode=settings.get("stale_pr_close_mode", "shadow"),
            enable_sprint_tagging=settings.get("enable_sprint_tagging", False),
            sprint_mode=settings.get("sprint_mode", "shadow"),
            sprint_anchor_date=settings.get("sprint_anchor_date", ""),
            sprint_anchor_number=settings.get("sprint_anchor_number", 0),
            sprint_length_days=settings.get("sprint_length_days", 14),
            enable_sprint_sweep=settings.get("enable_sprint_sweep", False),
            sprint_sweep_mode=settings.get("sprint_sweep_mode", "shadow"),
            sprint_sweep_lookback_sprints=settings.get(
                "sprint_sweep_lookback_sprints", 1
            ),
            enable_sprint_provision=settings.get("enable_sprint_provision", False),
            sprint_provision_mode=settings.get("sprint_provision_mode", "shadow"),
            sprint_provision_lookahead=settings.get("sprint_provision_lookahead", 2),
            sprint_board_id=settings.get("sprint_board_id", 0),
            sprint_name_format=settings.get("sprint_name_format", "Sprint {number}"),
            sprint_field=settings.get("sprint_field", ""),
            digest_enabled=settings.get("digest_enabled", False),
            digest_include_manual=settings.get("digest_include_manual", True),
            digest_include_reviews=settings.get("digest_include_reviews", True),
            digest_repo=settings.get("digest_repo", ""),
            digest_category_slug=settings.get("digest_category_slug", "announcements"),
            digest_title_prefix=settings.get("digest_title_prefix", "Weekly digest"),
            enable_manual_override=settings.get("enable_manual_override", True),
            manual_override_mode=settings.get("manual_override_mode", "shadow"),
            manual_override_fields=settings.get("manual_override_fields", ["status"])
            or ["status"],
            enable_team_assignment=settings.get("enable_team_assignment", False),
            team_assignment_mode=settings.get("team_assignment_mode", "shadow"),
            teams=_load_teams(settings.get("teams") or []),
            team_field=settings.get("team_field", ""),
            enable_rfc_epics=settings.get("enable_rfc_epics", False),
            rfc_epic_mode=settings.get("rfc_epic_mode", "shadow"),
            rfc_overrides=settings.get("rfc_overrides", {}) or {},
            epic_name_field=settings.get("epic_name_field", ""),
            enable_low_conf_email=settings.get("enable_low_conf_email", False),
            low_conf_email_mode=settings.get("low_conf_email_mode", "shadow"),
            low_conf_email_from=settings.get("low_conf_email_from", ""),
        )
        config.validate(
            require_runtime=require_runtime, extra_errors=unknown_key_errors
        )
        return config


_KNOWN_LLM_KEYS: Final[frozenset[str]] = frozenset(
    {"provider", "model", "vertex_project", "vertex_region"}
)

_KNOWN_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "roster_file",
        "github_repo",
        "jira_url",
        "jira_project_key",
        "jira_bot_aliases",
        "jira_components",
        "status_map",
        "issue_type",
        "container_issue_type",
        "merge_labels",
        "llm",
        "poll_interval_hours",
        "significant_comments_threshold",
        "ignore_pr_labels",
        "ignore_activity_authors",
        "bot_logins",
        "automation_opt_out_labels",
        "enable_estimation",
        "story_points_field",
        "contributors_field",
        "skills_dir",
        "enable_auto_create",
        "claim_mode",
        "stale_pr_close_days",
        "stale_pr_close_mode",
        "enable_sprint_tagging",
        "sprint_mode",
        "sprint_anchor_date",
        "sprint_anchor_number",
        "sprint_length_days",
        "enable_sprint_sweep",
        "sprint_sweep_mode",
        "sprint_sweep_lookback_sprints",
        "enable_sprint_provision",
        "sprint_provision_mode",
        "sprint_provision_lookahead",
        "sprint_board_id",
        "sprint_name_format",
        "sprint_field",
        "digest_enabled",
        "digest_include_manual",
        "digest_include_reviews",
        "digest_repo",
        "digest_category_slug",
        "digest_title_prefix",
        "enable_manual_override",
        "manual_override_mode",
        "manual_override_fields",
        "enable_team_assignment",
        "team_assignment_mode",
        "teams",
        "team_field",
        "enable_rfc_epics",
        "rfc_epic_mode",
        "rfc_overrides",
        "epic_name_field",
        "enable_low_conf_email",
        "low_conf_email_mode",
        "low_conf_email_from",
    }
)


def _unknown_key_errors(
    provided: dict[str, Any], known: frozenset[str], prefix: str
) -> list[str]:
    errors = []
    for key in sorted(set(provided) - known):
        close = difflib.get_close_matches(str(key), known, n=1)
        hint = f" (did you mean {close[0]!r}?)" if close else ""
        errors.append(f"unknown {prefix} key {key!r}{hint}")
    return errors


def _load_roster(config_dir: str, roster_file: str) -> list[TeamMember]:
    """ROSTER_YAML env var (YAML string) wins; else read roster_file relative
    to the config directory (R10). Missing roster file -> empty (validate
    reports at runtime); a malformed or empty ROSTER_YAML fails loudly."""
    raw = os.environ.get("ROSTER_YAML", "")
    if raw:
        entries = yaml.safe_load(raw) or []
        source = "ROSTER_YAML"
    else:
        roster_path = (
            roster_file
            if os.path.isabs(roster_file)
            else os.path.join(config_dir, roster_file)
        )
        try:
            with open(roster_path) as f:
                entries = yaml.safe_load(f) or []
        except FileNotFoundError:
            return []
        source = roster_file
    if not isinstance(entries, list) or not all(isinstance(m, dict) for m in entries):
        raise ValueError(
            f"{source} must be a YAML list of {{github, jira_email}} mappings"
        )
    if raw and not entries:
        raise ValueError("ROSTER_YAML is set but contains no members")
    return [
        TeamMember(
            github=str(m.get("github", "")),
            jira_email=str(m.get("jira_email", "")),
        )
        for m in entries
    ]


def _load_teams(entries: list[Any]) -> list[TeamSpec]:
    return [
        TeamSpec(
            name=str(t.get("name", "")),
            label=str(t.get("label", "")),
            team_id=str(t.get("team_id", "") or ""),
        )
        for t in entries
        if isinstance(t, dict)
    ]


def _load_llm(block: dict[str, Any]) -> LLMSettings:
    return LLMSettings(
        provider=block.get("provider", "anthropic"),
        model=block.get("model", ""),
        vertex_project=block.get(
            "vertex_project",
            os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
            or os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        ),
        vertex_region=block.get(
            "vertex_region", os.environ.get("CLOUD_ML_REGION", "us-east5")
        ),
    )

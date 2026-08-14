"""AppConfig loading/validation and CLI argument parsing."""

import os
from unittest.mock import patch

import pytest
from conftest import make_config
from upstream_jira_sync.cli import parse_args
from upstream_jira_sync.config import AppConfig, TeamSpec
from upstream_jira_sync.models import CanonicalStatus


class TestAppConfigValidation:
    def test_valid_config_passes(self):
        config = make_config()
        config.validate()
        assert len(config.team) == 1

    def test_empty_team_raises_at_runtime_only(self):
        config = make_config(team=[])
        with pytest.raises(ValueError, match="roster"):
            config.validate()
        config.validate(require_runtime=False)

    def test_multiple_errors_reported(self):
        config = make_config(github_token="", jira_token="")
        with pytest.raises(ValueError) as exc_info:
            config.validate()
        assert "GITHUB_TOKEN" in str(exc_info.value)
        assert "JIRA_API_TOKEN" in str(exc_info.value)

    def test_mode_validated_even_when_feature_disabled(self):
        config = make_config(enable_auto_create=False, claim_mode="on")
        with pytest.raises(ValueError, match="claim_mode"):
            config.validate()

    def test_rejects_unknown_override_field(self):
        config = make_config(manual_override_fields=["status", "priority"])
        with pytest.raises(ValueError, match="manual_override_fields"):
            config.validate()

    def test_accepts_customfield_pattern(self):
        config = make_config(manual_override_fields=["customfield_12345"])
        config.validate()


class TestStatusMap:
    def test_status_name_resolves_via_map(self):
        config = make_config(
            status_map={
                "todo": "New",
                "in_progress": "Doing",
                "review": "Code Review",
                "done": "Closed",
            }
        )
        config.validate()
        assert config.status_name(CanonicalStatus.REVIEW) == "Code Review"
        assert config.status_name(CanonicalStatus.DONE) == "Closed"
        assert config.active_status_names == ["New", "Doing", "Code Review"]

    def test_missing_state_rejected(self):
        config = make_config(status_map={"todo": "New"})
        with pytest.raises(ValueError, match="status_map is missing states"):
            config.validate()

    def test_unknown_state_rejected(self):
        status_map = {
            "todo": "To Do",
            "in_progress": "In Progress",
            "review": "In Review",
            "done": "Done",
            "blocked": "Blocked",
        }
        config = make_config(status_map=status_map)
        with pytest.raises(ValueError, match="unknown states"):
            config.validate()


class TestFeatureGatedCustomFields:
    def test_estimation_requires_story_points_field(self):
        config = make_config(enable_estimation=True)
        with pytest.raises(ValueError, match="story_points_field"):
            config.validate()
        config.story_points_field = "customfield_12345"
        config.validate()

    def test_sprint_features_require_sprint_field(self):
        config = make_config(
            enable_sprint_tagging=True,
            sprint_board_id=42,
            sprint_anchor_date="2026-06-02",
            sprint_anchor_number=34,
        )
        with pytest.raises(ValueError, match="sprint_field"):
            config.validate()
        config.sprint_field = "customfield_99001"
        config.validate()

    def test_team_field_required_when_team_ids_configured(self):
        teams = [TeamSpec(name="Team Alpha", label="team-alpha", team_id="uuid-1")]
        config = make_config(enable_team_assignment=True, teams=teams)
        with pytest.raises(ValueError, match="team_field"):
            config.validate()
        config.team_field = "customfield_11111"
        config.validate()

    def test_label_only_teams_do_not_require_team_field(self):
        teams = [TeamSpec(name="Team Alpha", label="team-alpha")]
        config = make_config(enable_team_assignment=True, teams=teams)
        config.validate()

    def test_malformed_field_id_rejected_even_when_feature_off(self):
        config = make_config(epic_name_field="Epic Name")
        with pytest.raises(ValueError, match="customfield_12345"):
            config.validate()

    def test_disabled_features_do_not_require_fields(self):
        config = make_config()
        config.validate()
        assert config.story_points_field == ""
        assert config.sprint_field == ""
        assert config.team_field == ""
        assert config.epic_name_field == ""


class TestWorkflowSettings:
    def test_issue_types_default_and_override(self):
        config = make_config()
        assert config.issue_type == "Story"
        assert config.container_issue_type == "Epic"
        custom = make_config(issue_type="Task", container_issue_type="Initiative")
        custom.validate()
        assert custom.container_issue_type == "Initiative"

    def test_team_assignment_requires_teams(self):
        config = make_config(enable_team_assignment=True, teams=[])
        with pytest.raises(ValueError, match="settings.teams"):
            config.validate()

    def test_duplicate_team_names_rejected(self):
        teams = [
            TeamSpec(name="Team Alpha", label="team-a"),
            TeamSpec(name="Team Alpha", label="team-b"),
        ]
        config = make_config(teams=teams)
        with pytest.raises(ValueError, match="unique"):
            config.validate()

    def test_vertex_provider_requires_project(self):
        config = make_config()
        config.llm.provider = "vertex"
        config.llm.vertex_project = ""
        with pytest.raises(ValueError, match="vertex_project"):
            config.validate()


class TestAppConfigLoad:
    def _write_config(self, tmp_path, extra_settings: str = "") -> str:
        (tmp_path / "team_roster.yaml").write_text(
            "- github: octocat\n  jira_email: octocat@example.com\n"
        )
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "settings:\n"
            "  github_repo: exampleorg/widgets\n"
            "  jira_url: https://yourcompany.atlassian.net\n"
            "  jira_project_key: PROJ\n"
            "  llm:\n"
            "    provider: anthropic\n"
            "    model: test-model\n" + extra_settings
        )
        return str(config_file)

    _ENV = {
        "GH_PAT": "token123",
        "JIRA_EMAIL": "bot@example.com",
        "JIRA_API_TOKEN": "jira-tok",
    }

    def test_load_from_yaml(self, tmp_path):
        path = self._write_config(
            tmp_path,
            extra_settings="  status_map:\n    review: Code Review\n",
        )
        with patch.dict(os.environ, self._ENV, clear=True):
            config = AppConfig.load(path)

        assert config.team[0].github == "octocat"
        assert config.github_token == "token123"
        assert config.llm.model == "test-model"
        # Partial status_map merges over the defaults.
        assert config.status_map["review"] == "Code Review"
        assert config.status_map["done"] == "Done"

    def test_roster_yaml_env_wins_over_roster_file(self, tmp_path):
        path = self._write_config(tmp_path)
        env = dict(self._ENV)
        env["ROSTER_YAML"] = "- github: hubot\n  jira_email: hubot@example.com\n"
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.load(path)

        assert [m.github for m in config.team] == ["hubot"]

    def test_missing_roster_fails_runtime_but_not_offline(self, tmp_path):
        path = self._write_config(tmp_path)
        os.remove(tmp_path / "team_roster.yaml")
        with patch.dict(os.environ, self._ENV, clear=True):
            with pytest.raises(ValueError, match="ROSTER_YAML"):
                AppConfig.load(path)
            config = AppConfig.load(path, require_runtime=False)
        assert config.team == []

    def test_malformed_roster_yaml_fails_even_offline(self, tmp_path):
        path = self._write_config(tmp_path)
        env = dict(self._ENV)
        env["ROSTER_YAML"] = "octocat: octocat@example.com\n"
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="ROSTER_YAML must be a YAML list"):
                AppConfig.load(path, require_runtime=False)

    def test_skills_dir_resolves_config_relative_and_must_exist(self, tmp_path):
        path = self._write_config(tmp_path, extra_settings="  skills_dir: prompts\n")
        with patch.dict(os.environ, self._ENV, clear=True):
            with pytest.raises(ValueError, match="skills_dir does not exist"):
                AppConfig.load(path)
            (tmp_path / "prompts").mkdir()
            config = AppConfig.load(path)
        assert config.skills_dir == str(tmp_path / "prompts")

    def test_unknown_settings_key_rejected_with_suggestion(self, tmp_path):
        path = self._write_config(tmp_path, extra_settings="  enable_estimaton: true\n")
        with patch.dict(os.environ, self._ENV, clear=True):
            with pytest.raises(ValueError, match="did you mean 'enable_estimation'"):
                AppConfig.load(path)

    def test_load_automation_opt_out_labels(self, tmp_path):
        path = self._write_config(
            tmp_path,
            extra_settings="  automation_opt_out_labels: [no-automation, no-sprint]\n",
        )
        with patch.dict(os.environ, self._ENV, clear=True):
            config = AppConfig.load(path)
        assert config.automation_opt_out_labels == ["no-automation", "no-sprint"]

    def test_automation_opt_out_labels_default_empty(self, tmp_path):
        path = self._write_config(tmp_path)
        with patch.dict(os.environ, self._ENV, clear=True):
            config = AppConfig.load(path)
        assert config.automation_opt_out_labels == []

    def test_load_teams_and_merge_labels(self, tmp_path):
        path = self._write_config(
            tmp_path,
            extra_settings=(
                "  merge_labels: [Merged]\n"
                "  teams:\n"
                "    - name: Team Alpha\n"
                "      label: team-alpha\n"
                "      team_id: uuid-1\n"
                "    - name: Team Beta\n"
                "      label: team-beta\n"
            ),
        )
        with patch.dict(os.environ, self._ENV, clear=True):
            config = AppConfig.load(path)

        assert config.merge_labels == ["Merged"]
        assert config.teams == [
            TeamSpec(name="Team Alpha", label="team-alpha", team_id="uuid-1"),
            TeamSpec(name="Team Beta", label="team-beta", team_id=""),
        ]


class TestParseArgs:
    def test_sync_all_flags(self):
        args = parse_args(
            [
                "sync",
                "--dry-run",
                "--member",
                "octocat",
                "--config",
                "ci.yaml",
                "--state-file",
                "test.json",
                "--since-hours",
                "336",
                "--mock-url",
                "http://localhost:9999",
            ]
        )
        assert args.dry_run is True
        assert args.member == "octocat"
        assert args.config == "ci.yaml"
        assert args.state_file == "test.json"
        assert args.since_hours == 336
        assert args.mock_url == "http://localhost:9999"

    def test_check_config_flags(self):
        args = parse_args(["check-config", "--config", "x.yaml", "--live"])
        assert args.command == "check-config"
        assert args.config == "x.yaml"
        assert args.live is True

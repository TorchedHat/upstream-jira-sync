from __future__ import annotations

import argparse
import logging
import sys

import requests
import yaml

from upstream_jira_sync.ai import (
    AITicketMatcher,
    IssueClaimClassifier,
    IssueDeduplicator,
    IssueSummarizer,
    RfcClassifier,
    StoryPointEstimator,
    TeamClassifier,
)
from upstream_jira_sync.config import AppConfig
from upstream_jira_sync.emailer import GmailNotifier
from upstream_jira_sync.github import GitHubClient
from upstream_jira_sync.jira import DryRunJiraClient, JiraClient
from upstream_jira_sync.llm.base import (
    LLMFatalError,
    load_provider,
    provider_load_error,
)
from upstream_jira_sync.models import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT
from upstream_jira_sync.orchestrator import SyncOrchestrator
from upstream_jira_sync.override_gate import ManualOverrideGate
from upstream_jira_sync.resolver import StatusResolver
from upstream_jira_sync.skill_loader import SKILL_TEMPLATE_VARS, SkillLoader
from upstream_jira_sync.state import SyncState

log = logging.getLogger(__name__)

_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="upstream-jira-sync",
        description="Sync upstream GitHub activity to Jira tickets.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Run a full sync pass.")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen without making any Jira writes.",
    )
    sync.add_argument(
        "--member",
        metavar="GITHUB_USERNAME",
        help="Run for a single team member only.",
    )
    sync.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml).",
    )
    sync.add_argument(
        "--state-file",
        default="sync_state.json",
        help="Path to state file for comment dedup (default: sync_state.json).",
    )
    sync.add_argument(
        "--since-hours",
        type=int,
        help="Override config.poll_interval_hours for this run "
        "(e.g. --since-hours 336 for a 2-week backfill).",
    )
    sync.add_argument(
        "--mock-url",
        default="",
        help="Route GitHub + Jira + LLM calls to a local mock server (dev only).",
    )
    sync.set_defaults(func=run_sync)

    check = subparsers.add_parser(
        "check-config", help="Validate config.yaml, roster, and skills offline."
    )
    check.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml).",
    )
    check.add_argument(
        "--live",
        action="store_true",
        help="Also run authenticated read-only checks against Jira and GitHub.",
    )
    check.set_defaults(func=run_check_config)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse args and dispatch to the selected subcommand."""
    _configure_logging()
    args = parse_args(argv)
    try:
        code = args.func(args)
    except LLMFatalError as exc:
        # A misconfigured provider fails the run instead of reporting success.
        log.error("LLM provider unusable, aborting: %s", exc)
        sys.exit(1)
    if code:
        sys.exit(code)


def run_sync(args: argparse.Namespace) -> int:
    """Load config, init services, run the sync pass."""
    if args.dry_run:
        log.info("[DRY RUN] No Jira writes will be made.\n")

    config = AppConfig.load(args.config)

    if args.since_hours is not None:
        config.poll_interval_hours = args.since_hours
        log.info("Overriding poll window to %d hours", args.since_hours)

    members = config.team
    if args.member:
        matched = [m for m in config.team if m.github == args.member]
        if not matched:
            known = ", ".join(m.github for m in config.team)
            log.error(
                "Unknown team member '%s'. Known members: %s",
                args.member,
                known,
            )
            return 1
        members = matched
        log.info("Filtered to member: %s", matched[0])

    mock_url = args.mock_url
    github_base_url = ""
    if mock_url:
        log.info("[MOCK] Using mock server at %s\n", mock_url)
        config.jira_url = mock_url
        config.llm.base_url = mock_url
        github_base_url = mock_url

    llm = load_provider(config.llm)
    _preflight_llm(llm)
    skill_loader = SkillLoader(override_dir=config.skills_dir)

    matcher = AITicketMatcher(llm=llm, skill_loader=skill_loader)
    resolver = StatusResolver(
        significant_comments_threshold=config.significant_comments_threshold
    )
    state = SyncState(path=args.state_file, read_only=args.dry_run)

    estimator: StoryPointEstimator | None = None
    if config.enable_estimation:
        estimator = StoryPointEstimator(llm=llm, skill_loader=skill_loader)

    classifier: IssueClaimClassifier | None = None
    summarizer: IssueSummarizer | None = None
    deduplicator: IssueDeduplicator | None = None
    if config.enable_auto_create:
        classifier = IssueClaimClassifier(llm=llm, skill_loader=skill_loader)
        summarizer = IssueSummarizer(llm=llm, skill_loader=skill_loader)
        deduplicator = IssueDeduplicator(llm=llm, skill_loader=skill_loader)

    team_classifier: TeamClassifier | None = None
    if config.enable_team_assignment:
        team_classifier = TeamClassifier(
            llm=llm, skill_loader=skill_loader, teams=config.teams
        )

    rfc_classifier: RfcClassifier | None = None
    if config.enable_rfc_epics:
        rfc_classifier = RfcClassifier(llm=llm, skill_loader=skill_loader)

    jira_cls = DryRunJiraClient if args.dry_run else JiraClient

    emailer: GmailNotifier | None = None
    if config.enable_low_conf_email and config.low_conf_email_mode == "auto":
        emailer = GmailNotifier(from_addr=config.low_conf_email_from)

    with (
        GitHubClient(
            token=config.github_token,
            repos=config.github_repo,
            merge_labels=tuple(config.merge_labels),
            base_url=github_base_url,
            ignore_activity_authors=config.ignore_activity_authors,
        ) as github,
        jira_cls(
            url=config.jira_url,
            email=config.jira_email,
            token=config.jira_token,
            sprint_field=config.sprint_field,
            container_issue_type=config.container_issue_type,
            open_status_names=tuple(config.active_status_names),
            automation_opt_out_labels=tuple(config.automation_opt_out_labels),
        ) as jira,
    ):
        if config.team_field:
            jira._team_field = config.team_field

        gate: ManualOverrideGate | None = None
        if config.enable_manual_override:
            gate = ManualOverrideGate(
                jira=jira,
                bot_email=config.jira_email,
                bot_account_id=config.jira_account_id,
                aliases=tuple(config.jira_bot_aliases),
            )

        orchestrator = SyncOrchestrator(
            config=config,
            github=github,
            jira=jira,
            matcher=matcher,
            resolver=resolver,
            state=state,
            estimator=estimator,
            classifier=classifier,
            summarizer=summarizer,
            deduplicator=deduplicator,
            override_gate=gate,
            team_classifier=team_classifier,
            rfc_classifier=rfc_classifier,
            emailer=emailer,
            members=members,
        )
        summary = orchestrator.run()

    log.info("\nSync complete -- %s", summary)
    return 1 if summary.errors > 0 else 0


def _preflight_llm(llm: object) -> None:
    """Validate key + model up front when the provider supports it (free
    endpoint, no tokens billed). Raises LLMFatalError on a misconfiguration."""
    preflight = getattr(llm, "preflight", None)
    if callable(preflight):
        preflight()


def run_check_config(args: argparse.Namespace) -> int:
    """Validate config, roster shape, and skill loading; --live adds API preflight."""
    failures = 0
    try:
        config = AppConfig.load(args.config, require_runtime=args.live)
        print(f"OK config: {args.config} loaded and validated")
    except FileNotFoundError as e:
        print(f"FAIL config: {e}")
        return 1
    except yaml.YAMLError as e:
        print(f"FAIL config: invalid YAML: {e}")
        return 1
    except ValueError as e:
        print(f"FAIL config: {e}")
        return 1
    if config.team:
        print(f"OK roster: {len(config.team)} member(s)")
    else:
        print("OK roster: empty (set ROSTER_YAML or roster_file before running sync)")

    loader = SkillLoader(override_dir=config.skills_dir)
    for skill_name in sorted(SKILL_TEMPLATE_VARS):
        try:
            loader.load(skill_name)
            print(f"OK skill: {skill_name}")
        except Exception as e:
            print(f"FAIL skill: {skill_name} -- {e}")
            failures += 1

    if problem := provider_load_error(config.llm.provider):
        print(f"FAIL llm provider: {problem}")
        failures += 1
    else:
        print(f"OK llm provider: {config.llm.provider}")

    if args.live:
        failures += _run_live_checks(config)

    return 1 if failures else 0


def _run_live_checks(config: AppConfig) -> int:
    failures = 0
    for name, check in _live_checks(config):
        try:
            problem = check()
        except Exception as e:
            problem = str(e)
        if problem:
            print(f"FAIL {name}: {problem}")
            failures += 1
        else:
            print(f"OK {name}")
    return failures


def _live_checks(config: AppConfig):
    base = config.jira_url.rstrip("/")
    auth = (config.jira_email, config.jira_token)

    def jira_get(path: str) -> requests.Response:
        resp = requests.get(f"{base}{path}", auth=auth, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp

    def check_project() -> str:
        jira_get(f"/rest/api/3/project/{config.jira_project_key}")
        return ""

    def check_statuses() -> str:
        # Per-project statuses, not the instance-global /rest/api/3/status: a
        # name existing in another project's workflow is useless for transitions.
        by_issue_type = jira_get(
            f"/rest/api/3/project/{config.jira_project_key}/statuses"
        ).json()
        entry = next(
            (
                t
                for t in by_issue_type
                if t.get("name", "").lower() == config.issue_type.lower()
            ),
            None,
        )
        if entry is None:
            return (
                f"issue type {config.issue_type!r} not found in project "
                f"{config.jira_project_key}"
            )
        known = {s["name"].lower() for s in entry.get("statuses", [])}
        missing = [v for v in config.status_map.values() if v.lower() not in known]
        if missing:
            return (
                f"status names not in the {config.issue_type!r} workflow of "
                f"project {config.jira_project_key}: {missing}"
            )
        return ""

    def check_fields() -> str:
        known = {f["id"] for f in jira_get("/rest/api/3/field").json()}
        configured = {
            "story_points_field": config.story_points_field,
            "sprint_field": config.sprint_field,
            "team_field": config.team_field,
            "epic_name_field": config.epic_name_field,
        }
        missing = [
            f"{name}={value}"
            for name, value in configured.items()
            if value and value not in known
        ]
        return f"custom fields not found: {missing}" if missing else ""

    def check_board() -> str:
        jira_get(f"/rest/agile/1.0/board/{config.sprint_board_id}")
        return ""

    def check_github() -> str:
        resp = requests.post(
            "https://api.github.com/graphql",
            json={"query": "query { viewer { login } }"},
            headers={"Authorization": f"Bearer {config.github_token}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        login = (payload.get("data") or {}).get("viewer", {}).get("login")
        if not login:
            return f"viewer query failed: {payload.get('errors')}"
        header = resp.headers.get("X-OAuth-Scopes")
        if header is None:
            print(
                f"  token user: @{login}; fine-grained token reports no scopes -- "
                "verify repo (and Discussions, if digest is enabled) access manually"
            )
            return ""
        scopes = {s.strip() for s in header.split(",") if s.strip()}
        required = {"repo"}
        if config.digest_enabled:
            required.add("read:discussion")
        missing = sorted(
            s
            for s in required
            if s not in scopes
            and not (s == "read:discussion" and "write:discussion" in scopes)
        )
        if missing:
            print(
                f"  WARN token user @{login} is missing scopes: "
                f"{', '.join(missing)} (granted: {header or 'none'})"
            )
        else:
            print(
                f"  OK token user @{login} has required scopes: "
                f"{', '.join(sorted(required))}"
            )
        return ""

    def check_llm() -> str:
        # Constructs the provider (raises if the key env var is missing) and,
        # for providers that support it, hits the free model-lookup endpoint.
        _preflight_llm(load_provider(config.llm))
        return ""

    checks = [
        (f"llm {config.llm.provider} model {config.llm.model}", check_llm),
        (f"jira project {config.jira_project_key}", check_project),
        ("jira statuses (status_map)", check_statuses),
        ("jira custom fields", check_fields),
    ]
    if config.sprint_features_enabled:
        checks.append((f"jira board {config.sprint_board_id}", check_board))
    checks.append(("github token", check_github))
    return checks

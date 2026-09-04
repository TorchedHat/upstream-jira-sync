from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Final, Literal

import requests

from upstream_jira_sync.config import AppConfig
from upstream_jira_sync.models import JiraTicketChange
from upstream_jira_sync.skill_loader import is_bot_author
from upstream_jira_sync.state import SyncState

log = logging.getLogger(__name__)

_GRAPHQL_URL: Final[str] = "https://api.github.com/graphql"


EventKind = Literal[
    "pr_linked",
    "story_points_set",
    "issue_claimed",
    "manual_transition",
    "manual_points_change",
    "ticket_created",
    "pr_orphaned",
    "co_author_noted",
]

_BOT_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "pr_linked",
        "story_points_set",
        "issue_claimed",
        "ticket_created",
        "pr_orphaned",
        "co_author_noted",
    }
)


@dataclass(frozen=True)
class DigestEvent:
    kind: EventKind
    ticket_key: str
    ticket_summary: str = ""
    old_value: str = ""
    new_value: str = ""
    timestamp: str = ""
    pr_url: str = ""
    source: str = "bot"
    assignee_email: str = (
        ""  # holds a GitHub handle after remapping; never rendered as an email (R10)
    )


@dataclass(frozen=True)
class DigestReport:
    window_start: datetime
    window_end: datetime
    events: tuple[DigestEvent, ...]

    @property
    def is_empty(self) -> bool:
        return len(self.events) == 0


def read_bot_events(
    state_path: str,
    window_start: datetime,
    team_github: set[str] | None = None,
) -> list[DigestEvent]:
    """Extract bot events from the state file's digest namespace (R11).

    SyncState owns the state schema (versioning, missing/corrupt files); this
    only maps raw entries to DigestEvents.
    """
    entries = SyncState(path=state_path, read_only=True).read_digest_events(
        window_start.isoformat()
    )
    roster = team_github or set()
    events: list[DigestEvent] = []

    for entry in entries:
        kind = entry.get("kind", "")
        ts = entry.get("observed_at", "")
        if kind not in _BOT_EVENT_KINDS or not ts:
            continue
        github_user = entry.get("github_user", "")
        assignee = github_user if github_user in roster else ""
        new_value = entry.get("new_value", "")
        if kind == "issue_claimed":
            new_value = entry.get("issue_url", "") or new_value
        events.append(
            DigestEvent(
                kind=kind,
                ticket_key=entry.get("ticket_key", ""),
                old_value=entry.get("old_value", ""),
                new_value=new_value,
                timestamp=ts,
                pr_url=entry.get("pr_url", ""),
                assignee_email=assignee,
            )
        )

    return events


def aggregate(
    bot_events: list[DigestEvent],
    jira_events: list[DigestEvent],
    window_start: datetime,
    window_end: datetime,
) -> DigestReport:
    merged = sorted(bot_events + jira_events, key=lambda e: e.timestamp)
    return DigestReport(
        window_start=window_start,
        window_end=window_end,
        events=tuple(merged),
    )


def jira_changes_to_events(
    changes: list[JiraTicketChange],
    bot_email: str,
    story_points_field: str,
) -> list[DigestEvent]:
    """Convert Jira changelog entries to DigestEvents; drop bot-authored changes."""
    points_fields = {story_points_field, "Story Points", "storyPoints"}
    events: list[DigestEvent] = []

    for c in changes:
        if is_bot_author(c.author_email, bot_email):
            continue

        if c.field == "Created":
            events.append(
                DigestEvent(
                    kind="ticket_created",
                    ticket_key=c.ticket_key,
                    ticket_summary=c.ticket_summary,
                    timestamp=c.changed_at,
                    source="jira",
                    assignee_email=c.ticket_assignee_email,
                )
            )
        elif c.field == "status":
            events.append(
                DigestEvent(
                    kind="manual_transition",
                    ticket_key=c.ticket_key,
                    ticket_summary=c.ticket_summary,
                    old_value=c.from_value,
                    new_value=c.to_value,
                    timestamp=c.changed_at,
                    source="jira",
                    assignee_email=c.ticket_assignee_email,
                )
            )
        elif c.field in points_fields:
            events.append(
                DigestEvent(
                    kind="manual_points_change",
                    ticket_key=c.ticket_key,
                    ticket_summary=c.ticket_summary,
                    old_value=c.from_value,
                    new_value=c.to_value,
                    timestamp=c.changed_at,
                    source="jira",
                    assignee_email=c.ticket_assignee_email,
                )
            )

    return events


def events_to_json(events: tuple[DigestEvent, ...] | list[DigestEvent]) -> str:
    return json.dumps([asdict(e) for e in events], indent=2)


def window_for(now: datetime, days: int = 7) -> tuple[datetime, datetime]:
    return now - timedelta(days=days), now


def render_markdown(
    report: DigestReport, narrative: str = "", review_section: str = ""
) -> str:
    start = report.window_start.strftime("%Y-%m-%d")
    end = report.window_end.strftime("%Y-%m-%d")

    lines = [f"# Weekly digest — {start} to {end}", ""]

    if report.is_empty:
        lines.append("_No ticket activity recorded in this window._")
        return "\n".join(lines)

    if narrative:
        lines += [f"> {narrative}", ""]

    lines += [
        f"_{len(report.events)} event(s) across {_assignee_count(report.events)} assignee(s). "
        "Expand your section below to see your tickets._",
        "",
    ]

    for assignee, events in _group_by_assignee(report.events):
        label = assignee or "Unassigned / bot-only"
        lines += [
            f"<details><summary><b>{label}</b> — {len(events)} event(s)</summary>",
            "",
            "| When | Ticket | Event | Details |",
            "| --- | --- | --- | --- |",
        ]
        lines += [_md_row(e) for e in events]
        lines += ["", "</details>", ""]

    if review_section:
        lines += [review_section, ""]

    lines.append("_Posted by upstream-jira-sync digest._")
    return "\n".join(lines)


def _group_by_assignee(
    events: tuple[DigestEvent, ...],
) -> list[tuple[str, list[DigestEvent]]]:
    """Alphabetical by GitHub handle; unassigned bucket last. Never sorted by
    event volume (R12)."""
    buckets: dict[str, list[DigestEvent]] = defaultdict(list)
    for e in events:
        buckets[e.assignee_email].append(e)
    assigned = sorted((k, v) for k, v in buckets.items() if k)
    unassigned = buckets.get("", [])
    return assigned + ([("", unassigned)] if unassigned else [])


def _assignee_count(events: tuple[DigestEvent, ...]) -> int:
    return len({e.assignee_email for e in events if e.assignee_email})


def _md_row(event: DigestEvent) -> str:
    when = event.timestamp.split("T")[0] if event.timestamp else "?"
    ticket = f"`{event.ticket_key}`" if event.ticket_key else "—"
    src = "" if event.source == "bot" else " _(manual)_"
    return f"| {when} | {ticket} | {event.kind}{src} | {_md_details(event)} |"


def _md_details(event: DigestEvent) -> str:
    if event.kind == "pr_linked":
        return f"→ {event.new_value}"
    if event.kind == "story_points_set":
        return f"set to {event.new_value} pts"
    if event.kind == "issue_claimed":
        return f"[claimed issue]({event.new_value})"
    if event.kind == "manual_transition":
        return f"{event.old_value} → {event.new_value}"
    if event.kind == "manual_points_change":
        return f"{event.old_value or '(unset)'} → {event.new_value} pts"
    if event.kind == "ticket_created":
        return event.ticket_summary or "new ticket"
    if event.kind == "pr_orphaned":
        prior = event.ticket_key or "(unknown)"
        return f"[PR]({event.pr_url}) was tracked by {prior}; Jira link missing -- needs attention"
    if event.kind == "co_author_noted":
        return f"co-author credited on [PR]({event.pr_url})"
    return ""


def _graphql(token: str, query: str, variables: dict) -> dict:
    """Raises on both HTTP failures and GraphQL `errors` payloads (which can return HTTP 200)."""
    resp = requests.post(
        _GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def post_digest_discussion(
    *,
    token: str,
    repo: str,
    category_slug: str,
    title: str,
    body: str,
) -> str:
    """Returns the created Discussion's URL."""
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise ValueError(f"Invalid repo '{repo}', expected owner/name")

    data = _graphql(
        token,
        """
        query($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            id
            discussionCategories(first: 25) { nodes { id slug } }
          }
        }
        """,
        {"owner": owner, "name": name},
    )

    repo_node = data.get("repository")
    if not repo_node:
        raise RuntimeError(f"Repository '{repo}' not found or token lacks access.")

    categories = repo_node["discussionCategories"]["nodes"]
    match = next((c for c in categories if c["slug"] == category_slug), None)
    if not match:
        slugs = [c["slug"] for c in categories]
        raise RuntimeError(
            f"Discussion category slug '{category_slug}' not found in {repo}. "
            f"Available: {slugs}"
        )

    created = _graphql(
        token,
        """
        mutation($repo: ID!, $cat: ID!, $title: String!, $body: String!) {
          createDiscussion(input: {
            repositoryId: $repo, categoryId: $cat,
            title: $title, body: $body
          }) { discussion { url } }
        }
        """,
        {"repo": repo_node["id"], "cat": match["id"], "title": title, "body": body},
    )

    return created["createDiscussion"]["discussion"]["url"]


def run_digest(
    *,
    config: AppConfig,
    state_path: str,
    days: int,
    post: bool,
    use_ai: bool,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    window_start, window_end = window_for(now, days=days)

    if not config.digest_enabled:
        log.info("digest_enabled is false in config; nothing to do.")
        return 0

    team_github = {m.github for m in config.team if m.github}
    email_to_github = {
        m.jira_email.lower(): m.github for m in config.team if m.jira_email
    }
    bot_events = read_bot_events(state_path, window_start, team_github)
    jira_events: list[DigestEvent] = []

    if config.digest_include_manual and config.jira_token:
        from upstream_jira_sync.jira import JiraClient

        jql = (
            f'project = "{config.jira_project_key}" '
            f'AND updated >= "{window_start.strftime("%Y-%m-%d %H:%M")}"'
        )
        with JiraClient(
            url=config.jira_url,
            email=config.jira_email,
            token=config.jira_token,
            container_issue_type=config.container_issue_type,
        ) as jira:
            changes = jira.search_ticket_changes(
                jql=jql,
                since_iso=window_start.isoformat(),
                story_points_field=config.story_points_field,
            )
        changes = [
            c
            for c in changes
            if c.ticket_assignee_email
            and c.ticket_assignee_email.lower() in email_to_github
        ]
        jira_events = jira_changes_to_events(
            changes, config.jira_email, config.story_points_field
        )
        jira_events = [
            replace(
                e,
                assignee_email=email_to_github.get(e.assignee_email.lower(), ""),
            )
            for e in jira_events
        ]

    report = aggregate(bot_events, jira_events, window_start, window_end)

    if report.is_empty:
        log.info("No events in the past %d day(s); skipping digest.", days)
        return 0

    narrative = _ai_narrative(config, report) if use_ai else ""

    review_section = ""
    if config.digest_include_reviews:
        from upstream_jira_sync.github import GitHubClient
        from upstream_jira_sync.review_activity import (
            fetch_review_stats,
            render_review_section,
        )
        from upstream_jira_sync.skill_loader import SkillLoader

        intro = SkillLoader(override_dir=config.skills_dir).load(
            "review_activity_intro"
        )
        try:
            with GitHubClient(
                token=config.github_token, repos=config.github_repo
            ) as github:
                stats = fetch_review_stats(
                    github, config.team, window_start, frozenset(config.bot_logins)
                )
            review_section = render_review_section(stats, intro)
        except Exception:
            log.warning("Review activity unavailable this week", exc_info=True)
            review_section = (
                f"{intro.strip()}\n\n"
                "_Review activity unavailable this week (GitHub query failed)._"
            )

    title = (
        f"{config.digest_title_prefix} — {window_end.strftime('%Y-%m-%d')} "
        f"({len(report.events)} event(s))"
    )
    body = render_markdown(report, narrative, review_section)

    if not post:
        log.info(
            "[DRY RUN] Would post to %s / %s",
            config.digest_repo,
            config.digest_category_slug,
        )
        print(body)
        return 0

    missing = _missing_discussion_fields(config)
    if missing:
        log.error("Cannot post digest — missing config: %s", ", ".join(missing))
        return 1

    url = post_digest_discussion(
        token=config.github_token,
        repo=config.digest_repo,
        category_slug=config.digest_category_slug,
        title=title,
        body=body,
    )
    log.info("Posted digest: %s", url)
    return 0


def _ai_narrative(config: AppConfig, report: DigestReport) -> str:
    from upstream_jira_sync.ai import WeeklyDigestSummarizer
    from upstream_jira_sync.llm import LLMFatalError, load_provider
    from upstream_jira_sync.skill_loader import SkillLoader

    try:
        provider = load_provider(config.llm.for_task("digest"))
        summarizer = WeeklyDigestSummarizer(
            provider, SkillLoader(override_dir=config.skills_dir)
        )
        return summarizer.summarize(events_to_json(report.events))
    except LLMFatalError:
        raise  # bad key / model / billing: fail the run, don't post a stub
    except Exception as exc:
        log.warning(
            "AI narrative unavailable (%s); falling back to delta table only.", exc
        )
        return ""


def _missing_discussion_fields(config: AppConfig) -> list[str]:
    required = {
        "github_token": config.github_token,
        "digest_repo": config.digest_repo,
        "digest_category_slug": config.digest_category_slug,
    }
    return [name for name, val in required.items() if not val]


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Weekly Jira digest → GitHub Discussion."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--state", default="sync_state.json")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--post", action="store_true", help="Actually post; default is dry-run."
    )
    parser.add_argument("--no-ai", action="store_true", help="Skip AI narrative.")
    args = parser.parse_args(sys.argv[1:])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-10s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from upstream_jira_sync.llm import LLMFatalError

    config = AppConfig.load(args.config)
    try:
        return run_digest(
            config=config,
            state_path=args.state,
            days=args.days,
            post=args.post,
            use_ai=not args.no_ai,
        )
    except LLMFatalError as exc:
        log.error("LLM provider unusable, aborting digest: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

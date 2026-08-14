from __future__ import annotations

import logging
import re
from datetime import date

from upstream_jira_sync.ai import TeamClassifier
from upstream_jira_sync.config import AppConfig
from upstream_jira_sync.github import GitHubClient
from upstream_jira_sync.jira import JiraClient
from upstream_jira_sync.models import (
    CanonicalStatus,
    JiraTicket,
    PullRequest,
    SprintRef,
    SyncSummary,
    TeamMember,
    pr_number_from_github_url,
    repo_from_github_url,
)
from upstream_jira_sync.sprint import (
    current_sprint_number,
    sprint_window,
    sprints_to_provision,
    sweep_cutoff_date,
)
from upstream_jira_sync.teams import primary_team_id, teams_to_labels

log = logging.getLogger(__name__)

_DIFF_PATH_RE = re.compile(r"^diff --git a/(\S+) b/\S+$", re.MULTILINE)

# Sentinel distinguishing "not yet resolved" from a resolved None.
_UNRESOLVED = object()


class TicketTagger:
    """Resolves the native sprint object for tickets; backfills team labels on old ones."""

    def __init__(
        self,
        config: AppConfig,
        github: GitHubClient,
        jira: JiraClient,
        team_classifier: TeamClassifier | None = None,
    ) -> None:
        self._config = config
        self._github = github
        self._jira = jira
        self._team_classifier = team_classifier
        self._pr_files_cache: dict[str, tuple[str, ...]] = {}
        self._sprint_cache: object = _UNRESOLVED

    def _opted_out(self, ticket: JiraTicket) -> bool:
        """True when a human has labelled this card off-limits to metadata writes.

        Matched case-insensitively: Jira labels are case-sensitive, so relying
        on an exact match would silently strip the protection off a card
        labelled `No-Sprint` when the config says `no-sprint`.
        """
        opt_out = {x.lower() for x in self._config.automation_opt_out_labels}
        return bool(opt_out and opt_out & {x.lower() for x in ticket.labels})

    def resolve_current_sprint(self) -> SprintRef | None:
        """Native sprint object for today via date-math, memoized once per run."""
        if self._sprint_cache is not _UNRESOLVED:
            return self._sprint_cache  # type: ignore[return-value]
        self._sprint_cache = self._resolve_current_sprint()
        return self._sprint_cache  # type: ignore[return-value]

    def _anchor_date(self) -> date | None:
        """Parsed sprint anchor, or None if unset or malformed (logged once)."""
        if not self._config.sprint_anchor_date:
            return None
        try:
            return date.fromisoformat(self._config.sprint_anchor_date)
        except ValueError:
            log.warning(
                "  Invalid sprint_anchor_date %r -- sprint features disabled",
                self._config.sprint_anchor_date,
            )
            return None

    def _resolve_current_sprint(self) -> SprintRef | None:
        if not self._config.enable_sprint_tagging:
            return None
        anchor = self._anchor_date()
        if anchor is None:
            return None
        number = current_sprint_number(
            anchor,
            date.today(),
            self._config.sprint_anchor_number,
            self._config.sprint_length_days,
        )
        if number is None:
            log.warning(
                "  Today is before sprint_anchor_date %s -- no current sprint",
                self._config.sprint_anchor_date,
            )
            return None
        name = self._config.sprint_name_format.format(number=number)
        return self._jira.get_sprint_by_name(self._config.sprint_board_id, name)

    def get_pr_files(self, repo: str, pr_number: int, pr_url: str) -> tuple[str, ...]:
        if pr_url in self._pr_files_cache:
            return self._pr_files_cache[pr_url]
        try:
            diff = self._github.get_pr_diff(repo, pr_number)
        except Exception as e:
            log.warning("  Failed to fetch diff for %s: %s", pr_url, e)
            paths: tuple[str, ...] = ()
        else:
            paths = tuple(_DIFF_PATH_RE.findall(diff))
        self._pr_files_cache[pr_url] = paths
        return paths

    def ordered_teams_for_pr(self, pr: PullRequest) -> list[str]:
        """Classified team names for this PR, primary owner first. Empty when unclassifiable."""
        if not (self._team_classifier and self._config.enable_team_assignment):
            return []
        if pr.number is None:
            return []
        try:
            repo = repo_from_github_url(pr.url)
        except ValueError:
            return []
        files = self.get_pr_files(repo, pr.number, pr.url)
        return self._team_classifier.classify_ordered(
            pr.title or "", pr.body or "", files
        )

    def team_assignment_for_pr(self, pr: PullRequest) -> tuple[list[str], str | None]:
        """(team labels, primary team id) from a single classification."""
        teams = self.ordered_teams_for_pr(pr)
        return (
            teams_to_labels(self._config.teams, teams),
            primary_team_id(self._config.teams, teams),
        )

    def backfill_team_labels(
        self,
        member: TeamMember,
        tickets: list[JiraTicket],
        summary: SyncSummary,
    ) -> None:
        """Route team-less tickets onto the dashboard: classify each (from its linked
        PR if any, else its summary/description) and set the native Team field + labels."""
        if not (self._team_classifier and self._config.enable_team_assignment):
            return

        for ticket in tickets:
            if ticket.team_id:
                continue
            if self._opted_out(ticket):
                log.info(
                    "  Skipping team backfill on %s: automation opt-out label",
                    ticket.key,
                )
                continue
            teams = self._classify_ticket_teams(ticket)
            if not teams:
                continue
            labels = teams_to_labels(self._config.teams, teams)
            team_id = primary_team_id(self._config.teams, teams)
            if self._config.team_assignment_mode == "shadow":
                log.info(
                    "  [TEAM-SHADOW] Would tag %s with %s (team %s)",
                    ticket.key,
                    labels,
                    team_id,
                )
                continue
            if labels and not set(labels) <= set(ticket.labels):
                merged = sorted(set(ticket.labels) | set(labels))
                self._jira.update_labels(ticket.key, merged)
                ticket.labels = merged
            if team_id and self._config.team_field:
                try:
                    self._jira.set_team(ticket.key, team_id, self._config.team_field)
                except Exception as e:
                    status = getattr(getattr(e, "response", None), "status_code", None)
                    if status == 400:
                        log.warning(
                            "  Team set rejected (field not editable?) on %s: %s",
                            ticket.key,
                            e,
                        )
                    else:
                        log.warning(
                            "  Failed setting team on %s", ticket.key, exc_info=True
                        )
                        summary.errors += 1
                    continue
                ticket.team_id = team_id
                summary.team_assigned += 1
            log.info("  Tagged %s team=%s labels=%s", ticket.key, team_id, labels)

    def _classify_ticket_teams(self, ticket: JiraTicket) -> list[str]:
        """Ordered teams for a ticket: from its linked PR if present, else its text.

        Container issues are always classified from their own summary/description:
        their scope spans many child PRs, so a single linked PR is not representative.
        """
        is_container = ticket.is_type(self._config.container_issue_type)
        pr_url = None if is_container else next(iter(ticket.pr_links), None)
        if pr_url:
            try:
                repo = repo_from_github_url(pr_url)
            except ValueError:
                return []
            pr_number = pr_number_from_github_url(pr_url)
            if pr_number is None:
                return []
            try:
                pr = self._github.get_pr(repo, pr_number)
            except Exception:
                return []
            if pr is None:
                return []
            return self.ordered_teams_for_pr(pr)
        return self._team_classifier.classify_ordered(
            ticket.summary or "", ticket.description or "", ()
        )

    def _sweep_cutoff(self) -> date | None:
        """Eligibility cutoff: cards must have entered an active status on/after this."""
        anchor = self._anchor_date()
        if anchor is None:
            return None
        return sweep_cutoff_date(
            anchor,
            date.today(),
            self._config.sprint_sweep_lookback_sprints,
            self._config.sprint_length_days,
        )

    def sweep_sprint(
        self,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        """Add recently-active cards to the current native sprint (idempotent, fail-closed).

        Eligibility (active status AND entered it within the lookback window) is
        decided by Jira's indexed history via JQL, so long-stalled zombie cards
        are excluded. Only the idempotent skip is done here.
        """
        if not (
            self._config.enable_sprint_tagging and self._config.enable_sprint_sweep
        ):
            return
        sprint = self.resolve_current_sprint()
        if sprint is None:
            return  # no named sprint on the board yet -- never clears
        cutoff = self._sweep_cutoff()
        if cutoff is None:
            return
        candidates = self._jira.get_sprint_sweep_candidates(
            member.jira_email,
            self._config.jira_project_key,
            [
                self._config.status_name(CanonicalStatus.IN_PROGRESS),
                self._config.status_name(CanonicalStatus.REVIEW),
            ],
            cutoff.isoformat(),
        )
        # The JQL already drops opted-out cards; re-checking here keeps the
        # guarantee if a caller supplies a client built without the clause.
        to_add = [
            t
            for t in candidates
            if sprint.id not in t.sprint_ids and not self._opted_out(t)
        ]
        if not to_add:
            return
        if self._config.sprint_sweep_mode == "shadow":
            log.info(
                "  [SPRINT-SWEEP-SHADOW] Would add %d ticket(s) to %s: %s",
                len(to_add),
                sprint.name,
                [t.key for t in to_add],
            )
            return
        for start in range(0, len(to_add), 50):
            batch = to_add[start : start + 50]
            keys = [t.key for t in batch]
            try:
                self._jira.add_issues_to_sprint(sprint.id, keys)
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 400:
                    log.warning("  Sprint add rejected (off-board?) %s: %s", keys, e)
                else:
                    log.warning(
                        "  Failed adding %s to %s",
                        keys,
                        sprint.name,
                        exc_info=True,
                    )
                    summary.errors += 1
                continue
            for t in batch:
                t.sprint_ids.add(sprint.id)
                summary.sprint_swept += 1
                log.info("  Added %s to sprint %s", t.key, sprint.name)

    def provision_future_sprints(self, summary: SyncSummary) -> None:
        """Pre-create the next `lookahead` sprints so cards can be staged early.

        Idempotent: each upcoming sprint is looked up by name first, only the
        missing ones are created. Fail-closed per sprint so one bad create does
        not abort the rest. Run once per sync pass (it is member-independent).
        """
        if not (
            self._config.enable_sprint_tagging and self._config.enable_sprint_provision
        ):
            return
        anchor = self._anchor_date()
        if anchor is None:
            return
        numbers = sprints_to_provision(
            anchor,
            date.today(),
            self._config.sprint_anchor_number,
            self._config.sprint_provision_lookahead,
            self._config.sprint_length_days,
        )
        for number in numbers:
            name = self._config.sprint_name_format.format(number=number)
            if self._jira.get_sprint_by_name(self._config.sprint_board_id, name):
                continue
            start, end = sprint_window(
                anchor,
                self._config.sprint_anchor_number,
                number,
                self._config.sprint_length_days,
            )
            if self._config.sprint_provision_mode == "shadow":
                log.info(
                    "  [SPRINT-PROVISION-SHADOW] Would create %s (%s to %s)",
                    name,
                    start.isoformat(),
                    end.isoformat(),
                )
                continue
            try:
                self._jira.create_sprint(self._config.sprint_board_id, name, start, end)
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (401, 403):
                    log.warning(
                        "  Sprint create denied (need Manage Sprints?) %s: %s", name, e
                    )
                else:
                    log.warning("  Failed creating sprint %s", name, exc_info=True)
                    summary.errors += 1
                continue
            summary.sprint_provisioned += 1

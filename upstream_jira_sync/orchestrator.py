from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from upstream_jira_sync.adf import AdfBuilder
from upstream_jira_sync.ai import (
    AITicketMatcher,
    DiscussionSummarizer,
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
from upstream_jira_sync.jira import JiraClient
from upstream_jira_sync.models import (
    CanonicalStatus,
    JiraTicket,
    PRLifecycleState,
    PROutcome,
    PRWithReview,
    PullRequest,
    SyncSummary,
    TeamMember,
    issue_number_from_github_url,
    pr_number_from_github_url,
    repo_from_github_url,
)
from upstream_jira_sync.override_gate import (
    ManualOverrideGate,
    status_blocked,
    story_points_blocked,
)
from upstream_jira_sync.resolver import StatusResolver, derive_lifecycle_state
from upstream_jira_sync.rfc_epics import RFC_TITLE_RE as _RFC_TITLE_RE
from upstream_jira_sync.rfc_epics import RfcEpicTracker
from upstream_jira_sync.skill_loader import SkillLoader
from upstream_jira_sync.state import SyncState
from upstream_jira_sync.tagging import TicketTagger

log = logging.getLogger(__name__)


def _parse_utc_timestamp(timestamp: str) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (Z or offset form). None when invalid."""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


class SyncOrchestrator:
    """Runs a full sync pass across all configured team members."""

    def __init__(
        self,
        config: AppConfig,
        github: GitHubClient,
        jira: JiraClient,
        matcher: AITicketMatcher,
        resolver: StatusResolver,
        state: SyncState,
        estimator: StoryPointEstimator | None = None,
        classifier: IssueClaimClassifier | None = None,
        summarizer: IssueSummarizer | None = None,
        deduplicator: IssueDeduplicator | None = None,
        *,
        override_gate: ManualOverrideGate | None = None,
        team_classifier: TeamClassifier | None = None,
        rfc_classifier: RfcClassifier | None = None,
        discussion_summarizer: DiscussionSummarizer | None = None,
        emailer: GmailNotifier | None = None,
        members: list[TeamMember] | None = None,
    ) -> None:
        self._config = config
        # Members to process this run (--member filter); config.team stays the
        # full roster so co-author recognition still sees every teammate.
        self._members = members if members is not None else config.team
        self._github = github
        self._jira = jira
        self._matcher = matcher
        self._resolver = resolver
        self._state = state
        self._estimator = estimator
        self._classifier = classifier
        self._summarizer = summarizer
        self._deduplicator = deduplicator
        self._override_gate = override_gate
        self._team_classifier = team_classifier
        self._rfc_classifier = rfc_classifier
        self._discussion_summarizer = discussion_summarizer
        self._emailer = emailer
        self._rfc_epics = RfcEpicTracker(
            config=config,
            jira=jira,
            state=state,
            github=github,
            summarizer=summarizer,
        )
        self._tagger = TicketTagger(
            config=config,
            github=github,
            jira=jira,
            team_classifier=team_classifier,
        )

    def run(self) -> SyncSummary:
        """Execute a full sync pass. Returns the accumulated summary."""
        summary = SyncSummary()

        self._tagger.provision_future_sprints(summary)

        for member in self._members:
            log.info("Processing %s", member)
            try:
                self._process_member(member, summary)
            except Exception:
                summary.errors += 1
                log.exception(
                    "  Fatal error processing %s -- continuing", member.github
                )

        return summary

    def _status_name(self, status: CanonicalStatus) -> str:
        return self._config.status_name(status)

    def _is_container(self, ticket: JiraTicket) -> bool:
        return ticket.is_type(self._config.container_issue_type)

    def _digest_event(self, kind: str, **fields: str) -> None:
        if self._config.digest_enabled:
            self._state.record_digest_event(kind, **fields)

    def _poll_window_start(self) -> str:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._config.poll_interval_hours
        )
        return cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    def _process_member(self, member: TeamMember, summary: SyncSummary) -> None:
        if self._classifier and self._config.enable_auto_create:
            self._process_issues(member, summary)

        pr_reviews = self._github.get_prs_by_user(
            member.github,
            self._poll_window_start(),
        )
        if not pr_reviews and self._config.stale_pr_close_days <= 0:
            return

        tickets = self._jira.get_open_tickets(
            member.jira_email, self._config.jira_project_key
        )
        if pr_reviews and not tickets:
            log.warning("  No open Jira tickets found for %s.", member)

        if self._override_gate is not None and tickets:
            self._override_gate.prefetch(
                [t.key for t in tickets],
                set(self._config.manual_override_fields),
            )

        for pr_with_review in pr_reviews:
            self._process_pr(pr_with_review, tickets, member, summary)

        self._close_stale_tickets(member, pr_reviews, tickets, summary)
        self._close_superseded_claim_tickets(member, tickets, summary)
        self._backfill_team_labels(member, tickets, summary)
        self._backfill_container_team_labels(member, summary)
        self._sweep_sprint(member, summary)

    def _process_issues(self, member: TeamMember, summary: SyncSummary) -> None:
        issues = self._github.get_issues(
            github_username=member.github,
            since_hours=self._config.poll_interval_hours,
            is_open=True,
        )

        for issue in issues:
            if self._state.is_issue_processed(issue.url):
                continue
            try:
                self._classify_and_handle_issue(issue, member, summary)
            except Exception:
                summary.errors += 1
                log.exception(
                    "  Error processing issue #%d for %s",
                    issue.number,
                    member.github,
                )

    def _classify_and_handle_issue(
        self,
        issue,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        if issue.state != "open":
            log.debug(
                "  Skipping closed issue #%d (%s)",
                issue.number,
                issue.state,
            )
            return

        if self._config.enable_rfc_epics and _RFC_TITLE_RE.match(issue.title or ""):
            verdict = "epic"
            if self._rfc_classifier is not None:
                verdict = self._rfc_classifier.classify(issue.title, issue.body)
                if verdict is None:
                    log.warning(
                        "  RFC check inconclusive for #%d; retrying next run",
                        issue.number,
                    )
                    return
            if verdict == "epic":
                self._state.record_issue_classification(
                    issue.url,
                    "rfc",
                    "RFC title detected",
                )
                epic_key = self._ensure_epic_for_rfc(
                    issue.url, issue.title, issue.body, member
                )
                if epic_key:
                    self._state.set_issue_ticket(issue.url, epic_key)
                return
            log.info(
                "  Issue #%d carries an RFC title but is single-deliverable scope; "
                "continuing as a normal issue",
                issue.number,
            )

        claim = self._classifier.classify(
            issue,
            issue.latest_comment,
            member.github,
        )
        self._state.record_issue_classification(
            issue.url,
            claim.intent,
            claim.reason,
        )

        if claim.intent != "claiming":
            return

        self._digest_event(
            "issue_claimed", issue_url=issue.url, github_user=member.github
        )
        self._ensure_jira_for_issue(
            issue_number=issue.number,
            issue_title=issue.title,
            issue_url=issue.url,
            issue_body=issue.body,
            member=member,
            summary=summary,
        )

    def _ensure_epic_for_rfc(
        self,
        rfc_url: str,
        rfc_title: str,
        rfc_body: str,
        member: TeamMember,
    ) -> str | None:
        return self._rfc_epics.ensure_epic(rfc_url, rfc_title, rfc_body, member)

    def _rfc_parent_for_pr(self, pr: PullRequest, member: TeamMember) -> str | None:
        return self._rfc_epics.parent_for_pr(pr, member)

    def _maybe_reparent_under_rfc(
        self,
        pr: PullRequest,
        ticket: JiraTicket,
        member: TeamMember,
    ) -> None:
        self._rfc_epics.maybe_reparent(
            pr, ticket, member, override_gate=self._override_gate
        )

    def _build_creation_fields(self, member: TeamMember, source: Any) -> dict:
        """Assemble creation-only Jira fields (team labels + Team field, RFC parent); never sent on updates."""
        fields: dict = {}
        source_pr = source if isinstance(source, PullRequest) else None
        if source_pr is not None:
            team_labels, team_id = self._tagger.team_assignment_for_pr(source_pr)
            if (
                team_labels or team_id
            ) and self._config.team_assignment_mode == "shadow":
                log.info(
                    "  [TEAM-SHADOW] Would tag %s with %s (team %s)",
                    source_pr.url,
                    team_labels,
                    team_id,
                )
                team_labels, team_id = [], None
            if team_labels:
                existing = fields.get("labels", [])
                fields["labels"] = sorted(set(existing) | set(team_labels))
            if team_id and self._config.team_field:
                fields[self._config.team_field] = team_id

        if self._config.enable_rfc_epics and source_pr is not None:
            epic_key = self._rfc_parent_for_pr(source_pr, member)
            if epic_key:
                fields["parent"] = {"key": epic_key}
        return fields

    def _ensure_jira_for_issue(
        self,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        issue_body: str,
        member: TeamMember,
        summary: SyncSummary,
        source: str = "",
        pr: PullRequest | None = None,
    ) -> None:
        """Dedup check + shadow/auto gate + create Jira. Shared by comment claims and PR-linked issues."""
        existing = self._jira.find_tracking_ticket(
            issue_url,
            self._config.jira_project_key,
        )
        # Check for existing ticket by upstream issue number (prevents release-to-release dupes)
        if (
            not existing
            and self._config.upstream_issue_number_field
        ):
            existing = self._jira.find_ticket_by_custom_field(
                self._config.jira_project_key,
                self._config.upstream_issue_number_field,
                str(issue_number),
            )

        if not existing and self._deduplicator:
            candidates = self._jira.find_candidate_tickets(
                self._config.jira_project_key, issue_title
            )
            match = self._deduplicator.find_existing(
                issue_title, issue_body, candidates
            )
            existing = match.ticket if match else None
        if existing:
            self._link_existing_ticket(
                existing, issue_number, issue_title, issue_url, pr
            )
            self._state.set_issue_ticket(issue_url, existing.key)
            return

        issue_summary = ""
        if self._summarizer:
            issue_summary = self._summarizer.summarize(issue_title, issue_body)
        fallback = source or f"Claimed by @{member.github}."
        description = issue_summary or fallback

        if self._config.claim_mode == "shadow":
            estimate_preview = ""
            if self._estimator:
                preview_ticket = JiraTicket(
                    key="SHADOW",
                    summary=issue_title,
                    status=self._status_name(CanonicalStatus.TODO),
                    url="",
                )
                points = (
                    self._estimator.estimate(pr, preview_ticket)
                    if pr
                    else self._estimator.estimate_from_issue(
                        preview_ticket, issue_title, issue_body
                    )
                )
                if points is not None:
                    estimate_preview = f"\n    Story Points: {points} (provisional)"
            log.info(
                "  [SHADOW] Would create Jira for issue #%d: %s\n"
                "    Summary: %s\n"
                "    Assignee: %s\n"
                "    Project: %s%s",
                issue_number,
                issue_title[:60],
                description,
                member,
                self._config.jira_project_key,
                estimate_preview,
            )
            summary.would_create += 1
            return

        desc = AdfBuilder.issue_description(
            issue_title,
            issue_url,
            description,
        )
        extra_fields = self._build_creation_fields(member, pr) or None
        ticket = self._jira.create_ticket(
            project_key=self._config.jira_project_key,
            summary=issue_title[:255],
            description_adf=desc,
            assignee_email=member.jira_email,
            extra_fields=extra_fields,
            issuetype=self._config.issue_type,
            components=self._config.jira_components or None,
            initial_status_name=self._status_name(CanonicalStatus.TODO),
        )
        summary.issues_created += 1
        self._state.set_issue_ticket(issue_url, ticket.key)

        # Post-creation customizations
        self._post_create_ticket(ticket, issue_url, issue_number)

        self._digest_event(
            "ticket_created",
            ticket_key=ticket.key,
            issue_url=issue_url,
            github_user=member.github,
        )
        self._add_to_current_sprint(ticket, summary)

        if pr:
            self._add_pr_remote_links(pr, ticket)
            self._try_estimate(pr, ticket, member, summary)
        else:
            self._link_issue_to_ticket(ticket, issue_number, issue_title, issue_url)
            self._try_estimate_from_issue(ticket, issue_title, issue_body, summary)

    def _link_issue_to_ticket(
        self,
        ticket: JiraTicket,
        issue_number: int,
        issue_title: str,
        issue_url: str,
    ) -> None:
        try:
            self._jira.add_remote_link(
                ticket,
                issue_url,
                f"Issue #{issue_number}: {issue_title}",
                relationship="GitHub issue",
            )
        except Exception:
            log.exception("  Failed to link issue #%d to %s", issue_number, ticket.key)

    def _post_create_ticket(
        self,
        ticket: JiraTicket,
        issue_url: str,
        issue_number: int,
    ) -> None:
        """Post-creation customizations: deduplication tracking, watchers, discussion summary."""
        # Store upstream issue number in custom field for deduplication checks
        if self._config.upstream_issue_number_field:
            try:
                self._jira._request(
                    "PUT",
                    f"{self._jira._base}/rest/api/3/issue/{ticket.key}",
                    params={"notifyUsers": "false"},
                    json={
                        "fields": {
                            self._config.upstream_issue_number_field: str(issue_number)
                        }
                    },
                )
                log.info(
                    "  %s: set upstream_issue_number to %d",
                    ticket.key,
                    issue_number,
                )
            except Exception:
                log.exception(
                    "  Failed to set upstream_issue_number on %s",
                    ticket.key,
                )

        # Add default watchers
        if self._config.default_watchers:
            try:
                self._jira.add_watchers(ticket.key, self._config.default_watchers)
            except Exception:
                log.exception("  Failed to add watchers to %s", ticket.key)

        # Post discussion summary as Jira comment if discussion summarizer is available
        if self._discussion_summarizer:
            self._post_discussion_summary(ticket, issue_url, issue_number)

    def _post_discussion_summary(
        self,
        ticket: JiraTicket,
        issue_url: str,
        issue_number: int,
    ) -> None:
        """Fetch upstream issue comments, summarize, and post as Jira comment."""
        repo = repo_from_github_url(issue_url)
        if not repo:
            log.warning("  Could not extract repo from %s", issue_url)
            return

        try:
            # Fetch recent comments from the upstream issue
            comments = self._github.get_issue_comments(repo, issue_number, max_comments=10)
            if not comments:
                log.info("  No comments found on issue #%d", issue_number)
                return

            # Summarize the discussion
            summary = self._discussion_summarizer.summarize(comments)
            if not summary:
                log.info("  Could not summarize discussion for issue #%d", issue_number)
                return

            # Post summary as Jira comment
            comment_text = (
                f"## Upstream Discussion Summary\n\n{summary}\n\n"
                f"[View full discussion]({issue_url})"
            )
            self._jira.post_note(ticket, comment_text)
            log.info(
                "  %s: posted upstream discussion summary for issue #%d",
                ticket.key,
                issue_number,
            )
        except Exception:
            log.exception(
                "  Failed to post discussion summary for issue #%d to %s",
                issue_number,
                ticket.key,
            )

    def _link_existing_ticket(
        self,
        ticket: JiraTicket,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        pr: PullRequest | None,
    ) -> None:
        """An existing ticket already tracks this issue: backfill GitHub links and move on."""
        log.info(
            "  Issue #%d already tracked by %s -- skipping creation.",
            issue_number,
            ticket.key,
        )
        if pr:
            self._add_pr_remote_links(pr, ticket)
        else:
            self._link_issue_to_ticket(ticket, issue_number, issue_title, issue_url)

    def _create_jira_from_linked_issues(
        self,
        pr: PullRequest,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        for linked in pr.linked_issues:
            # A member PR that closing-references the issue outranks a prior
            # not_claiming comment verdict. Every other classification still
            # dedups.
            if self._state.is_issue_processed(
                linked.url
            ) and not self._state.is_issue_not_claiming(linked.url):
                continue
            try:
                self._state.record_issue_classification(
                    linked.url,
                    "pr_linked",
                    f"Referenced by PR #{pr.number}",
                )
                self._ensure_jira_for_issue(
                    issue_number=linked.number,
                    issue_title=linked.title,
                    issue_url=linked.url,
                    issue_body=linked.body,
                    member=member,
                    summary=summary,
                    source=f"PR #{pr.number} references this issue.",
                    pr=pr,
                )
            except Exception:
                summary.errors += 1
                log.exception(
                    "  Error creating Jira for issue #%d (from PR #%d)",
                    linked.number,
                    pr.number,
                )

    def _try_estimate(
        self,
        pr: PullRequest,
        ticket: JiraTicket,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        """Estimate story points if enabled and not already done. Isolated from sync flow."""
        if not self._estimator or self._state.is_estimated(pr.url, ticket.key):
            return
        if self._is_container(ticket):
            log.info(
                "  Skipping story-point estimate on %s %s",
                self._config.container_issue_type,
                ticket.key,
            )
            return
        try:
            points = self._estimator.estimate(pr, ticket)
            if points is None:
                return
            if self._story_points_blocked_by_override(ticket):
                return
            self._jira.set_story_points(
                ticket,
                points,
                self._config.story_points_field,
            )
            self._state.record_estimation(pr.url, ticket.key, points)
            self._digest_event(
                "story_points_set",
                ticket_key=ticket.key,
                pr_url=pr.url,
                github_user=member.github,
                new_value=str(points),
            )
            summary.estimated += 1
        except Exception:
            log.exception(
                "  Estimation failed for %s (PR #%d) -- continuing",
                ticket.key,
                pr.number,
            )

    def _story_points_blocked_by_override(self, ticket: JiraTicket) -> bool:
        return story_points_blocked(self._override_gate, self._config, ticket)

    def _status_blocked_by_override(
        self, ticket: JiraTicket, current_intent: str
    ) -> bool:
        return status_blocked(
            self._override_gate, self._config, self._state, ticket, current_intent
        )

    def _refresh_pr_state_snapshot(
        self,
        ticket: JiraTicket,
        pr: PullRequest,
        pr_state: str,
        resolved_status: str,
    ) -> None:
        if self._override_gate is None:
            return
        self._state.record_pr_state_snapshot(
            ticket_key=ticket.key,
            pr_state=pr_state,
            pr_url=pr.url,
            resolved_status=resolved_status,
        )

    def _try_estimate_from_issue(
        self,
        ticket: JiraTicket,
        issue_title: str,
        issue_body: str,
        summary: SyncSummary,
    ) -> None:
        """Provisional claim-time estimate. Refined later when PR opens.

        Intentionally ungated: this fires on a brand-new ticket created in this same
        sync run, so there is no prior history to honor.
        """
        if not self._estimator:
            return
        if self._is_container(ticket):
            log.info(
                "  Skipping story-point estimate on %s %s",
                self._config.container_issue_type,
                ticket.key,
            )
            return
        try:
            points = self._estimator.estimate_from_issue(
                ticket, issue_title, issue_body
            )
            if points is not None:
                self._jira.set_story_points(
                    ticket,
                    points,
                    self._config.story_points_field,
                )
                summary.estimated += 1
        except Exception:
            log.exception(
                "  Provisional estimation failed for %s -- continuing",
                ticket.key,
            )

    def _add_pr_remote_links(
        self,
        pr: PullRequest,
        ticket: JiraTicket,
    ) -> None:
        """Add the PR (and any linked issues) as web links on the Jira ticket."""
        try:
            self._jira.add_remote_link(
                ticket,
                pr.url,
                f"PR #{pr.number}: {pr.title}",
                relationship="pull request",
            )
            for linked in pr.linked_issues:
                self._jira.add_remote_link(
                    ticket,
                    linked.url,
                    f"Issue #{linked.number}: {linked.title}",
                    relationship="GitHub issue",
                )
        except Exception:
            log.exception(
                "  Failed to add remote links for %s (PR #%d) -- continuing",
                ticket.key,
                pr.number,
            )

    def _process_pr(
        self,
        pr_review: PRWithReview,
        tickets: list[JiraTicket],
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        pr = pr_review.pr

        if pr.draft:
            summary.record_pr_outcome(PROutcome.DRAFT)
            log.info("  Skipping draft PR #%d", pr.number)
            return

        # Skip open PRs flagged with an ignored label (e.g. "Stale"): the label bumps
        # updatedAt and drags an abandoned PR back into the poll window. Merged/closed
        # PRs still flow through so a terminal event always wins.
        if pr.state == "open" and pr.has_any_label(self._config.ignore_pr_labels):
            summary.record_pr_outcome(PROutcome.IGNORED_LABEL)
            log.info(
                "  Skipping PR #%d -- ignored label(s) present: %s",
                pr.number,
                self._config.ignore_pr_labels,
            )
            return

        # A bot comment also bumps updatedAt and drags the PR back into the poll
        # window; with no human activity in the window there is nothing to sync,
        # and a stale-closed ticket must not be reopened into Review by bot noise.
        if pr.state == "open" and self._human_activity_expired(pr):
            summary.record_pr_outcome(PROutcome.BOT_ACTIVITY)
            log.info(
                "  Skipping PR #%d -- no human activity since %s (bot-only updates)",
                pr.number,
                pr.human_activity_at[:10],
            )
            return

        # Consult state before paying for the AI matcher: a PR already linked
        # to a ticket keeps that ticket, and a PR the matcher already declined
        # is only re-asked once a human touches it again (or the memory ages
        # out with the state TTL).
        match = None
        linked = self._linked_ticket(pr, tickets)
        if linked is not None:
            ticket = linked
            match_confidence = "state"
            match_reason = "Reused the ticket previously linked to this PR."
            summary.record_pr_outcome(PROutcome.MATCHED)
            log.info(
                "  PR #%d already linked to %s -- skipping AI match",
                pr.number,
                ticket.key,
            )
        elif tickets and self._state.is_low_conf_unchanged(
            pr.url, pr.human_activity_at
        ):
            log.info(
                "  PR #%d unchanged since last low-confidence match -- skipping AI match",
                pr.number,
            )
        elif tickets:
            match = self._matcher.find_best(pr, tickets)
            if match is None:
                self._state.record_low_conf_match(pr.url, pr.human_activity_at)

        if match:
            ticket = match.ticket
            match_confidence, match_reason = match.confidence, match.reason
            summary.record_pr_outcome(PROutcome.MATCHED)
            self._state.clear_low_conf_match(pr.url)
            self._maybe_reparent_under_rfc(pr, ticket, member)
        elif linked is None:
            summary.record_pr_outcome(
                PROutcome.NO_TICKETS if not tickets else PROutcome.LOW_CONF
            )
            ticket = self._auto_create_for_unmatched_pr(pr, member, summary)
            if ticket is None:
                self._maybe_ping_low_confidence(pr, member)
                self._note_co_authors(pr, None, summary)
                return
            match_confidence = "auto"
            match_reason = "Auto-created -- open upstream PR with no tracking ticket."

        self._try_estimate(pr, ticket, member, summary)
        self._add_pr_remote_links(pr, ticket)
        self._note_co_authors(pr, ticket, summary)

        try:
            target_status = self._resolver.resolve(
                pr, pr_review.review_decision, pr_review.changes_requested_count
            )

            if pr.is_cancelled:
                self._handle_cancelled_pr(pr, ticket, member, summary)
                return

            # PR is active or merged: any earlier cancel debounce no longer applies.
            self._state.clear_pr_cancel_seen(pr.url, ticket.key)

            lifecycle = derive_lifecycle_state(
                pr, pr_review.review_decision, pr_review.changes_requested_count
            )
            if not self._status_blocked_by_override(ticket, target_status.value):
                if self._apply_transition(ticket, target_status):
                    summary.transitioned += 1
                self._refresh_pr_state_snapshot(
                    ticket, pr, lifecycle.value, target_status.value
                )

            if not self._state.is_commented(pr.url, ticket.key):
                self._jira.post_comment(
                    ticket,
                    pr,
                    self._status_name(target_status),
                    match_confidence=match_confidence,
                    match_reason=match_reason,
                )
                self._state.record_comment(
                    pr.url,
                    ticket.key,
                    target_status.value,
                    match_confidence=match_confidence,
                    match_reason=match_reason,
                )
                self._digest_event(
                    "pr_linked",
                    ticket_key=ticket.key,
                    pr_url=pr.url,
                    github_user=member.github,
                    new_value=target_status.value,
                )
                summary.commented += 1
            else:
                summary.comment_dedup += 1
                log.info(
                    "  PR #%d already linked on %s -- skipping comment.",
                    pr.number,
                    ticket.key,
                )

        except Exception:
            summary.errors += 1
            log.exception(
                "  Error processing PR #%d against %s",
                pr.number,
                ticket.key,
            )

    def _linked_ticket(
        self, pr: PullRequest, tickets: list[JiraTicket]
    ) -> JiraTicket | None:
        """Return the candidate ticket state already links this PR to, if any.

        Falls through to the matcher when the remembered ticket is not among
        the member's current candidates (closed, reassigned, or pruned).
        """
        key = self._state.get_linked_ticket_key(pr.url)
        if not key:
            return None
        for ticket in tickets:
            if ticket.key == key:
                return ticket
        return None

    def _roster_co_authors(self, pr: PullRequest) -> list[TeamMember]:
        """Team-roster members (other than the PR author) that authored commits on the PR."""
        commit_authors = {a.lower() for a in pr.commit_authors}
        return sorted(
            (
                m
                for m in self._config.team
                if m.github.lower() in commit_authors
                and m.github.lower() != pr.author.lower()
            ),
            key=lambda m: m.github.lower(),
        )

    def _note_co_authors(
        self,
        pr: PullRequest,
        ticket: JiraTicket | None,
        summary: SyncSummary,
    ) -> None:
        """Post an attributed Jira note per team co-author on the PR's tracking ticket.

        Co-authors share the PR's one ticket instead of getting a duplicate; the note
        plus the co_contributions state record is their credit."""
        co_authors = self._roster_co_authors(pr)
        if not co_authors:
            return
        if ticket is None:
            # Open unmatched PRs already had a tracking-ticket lookup that declined
            # (shadow mode, linked issues, orphaned); don't re-query and override it.
            if pr.state == "open":
                return
            ticket = self._jira.find_tracking_ticket(
                pr.url, self._config.jira_project_key
            )
        if ticket is None:
            log.info(
                "  PR #%d has team co-author(s) %s but no tracking ticket",
                pr.number,
                [m.github for m in co_authors],
            )
            return
        for member in co_authors:
            login = member.github
            if self._state.is_co_contribution_noted(pr.url, ticket.key, login):
                continue
            account_id = self._jira.resolve_account_id(member.jira_email)
            try:
                if account_id:
                    self._jira.post_mention_note(
                        ticket,
                        "Multi-author PR: ",
                        account_id,
                        f" contributed commits to PR #{pr.number}.",
                        display_name=self._jira.resolve_display_name(member.jira_email),
                    )
                else:
                    self._jira.post_note(
                        ticket,
                        f"Multi-author PR: @{login} contributed commits to "
                        f"PR #{pr.number} ({pr.url}).",
                    )
                self._state.record_co_contribution(pr.url, ticket.key, login)
            except Exception:
                log.exception(
                    "  Failed to note co-author @%s on %s -- continuing",
                    login,
                    ticket.key,
                )
                continue
            summary.co_authors_noted += 1
            self._digest_event(
                "co_author_noted",
                ticket_key=ticket.key,
                pr_url=pr.url,
                github_user=login,
            )
            self._add_contributor_field(ticket, member, account_id)
            log.info(
                "  Noted co-author @%s on %s (PR #%d)", login, ticket.key, pr.number
            )

    def _add_contributor_field(
        self, ticket: JiraTicket, member: TeamMember, account_id: str | None
    ) -> None:
        """Best-effort append to the contributors picker, only when the co-author note
        was newly posted (re-adding an existing user to a multi-user picker can 400).
        Failures never raise out or affect counters (mirrors the Team-field precedent)."""
        if not self._config.contributors_field:
            return
        if not account_id:
            log.warning(
                "  Could not resolve Jira account for %s -- note-only credit",
                member.jira_email,
            )
            return
        try:
            self._jira.add_contributor(
                ticket.key, account_id, self._config.contributors_field
            )
        except Exception:
            log.warning(
                "  Failed to add contributor @%s to %s -- note-only credit",
                member.github,
                ticket.key,
                exc_info=True,
            )

    def _maybe_ping_low_confidence(self, pr: PullRequest, member: TeamMember) -> None:
        """Email the PR author when the bot could not track their PR. Once per PR."""
        if not self._config.enable_low_conf_email:
            return
        if self._state.is_low_conf_pinged(pr.url):
            return
        template = SkillLoader(override_dir=self._config.skills_dir).load(
            "low_conf_email"
        )
        rendered = template.format(
            pr_number=pr.number,
            pr_title=pr.title,
            pr_url=pr.url,
            github=member.github,
        ).strip()
        first_line, _, body = rendered.partition("\n")
        subject = first_line.removeprefix("Subject:").strip()
        body = body.strip() + "\n"
        if self._config.low_conf_email_mode == "shadow" or self._emailer is None:
            log.info(
                "  [EMAIL-SHADOW] Would email %s about untracked PR #%d",
                member,
                pr.number,
            )
            return
        try:
            self._emailer.send(member.jira_email, subject, body)
        except Exception:
            log.exception("  Failed to email %s about PR #%d", member, pr.number)
            return
        self._state.record_low_conf_ping(pr.url)

    def _apply_transition(
        self, ticket: JiraTicket, target_status: CanonicalStatus
    ) -> bool:
        """Transition the ticket and update its in-memory status. Returns True if moved.

        Container issues are never transitioned by PR-driven sync; this is the backstop
        if one ever reaches a transition path despite the candidate-pool queries
        excluding them.
        """
        status_name = self._status_name(target_status)
        if self._is_container(ticket):
            log.info(
                "  Refusing to transition %s %s to '%s' -- container issues are not PR-driven",
                self._config.container_issue_type,
                ticket.key,
                status_name,
            )
            return False
        if self._jira.transition_ticket(ticket, status_name):
            ticket.status = status_name
            return True
        return False

    def _handle_cancelled_pr(
        self,
        pr: PullRequest,
        ticket: JiraTicket,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        """Close the ticket for an explicitly cancelled (closed-unmerged) PR.

        Gated by stale_pr_close_mode (the shared dead-PR-closing gate) and debounced
        one run so upstream close/reopen churn doesn't flap the ticket. A revived PR
        reopens the ticket via the normal Review path on a later run.
        """
        if self._config.stale_pr_close_mode == "shadow":
            log.info(
                "  [SHADOW] Would close %s -- PR #%d cancelled (closed without merge)",
                ticket.key,
                pr.number,
            )
            return

        if not self._state.record_pr_cancel_seen(pr.url, ticket.key):
            log.info(
                "  PR #%d closed without merge -- will close %s next run if still closed",
                pr.number,
                ticket.key,
            )
            return

        if self._status_blocked_by_override(ticket, CanonicalStatus.DONE.value):
            return

        done_name = self._status_name(CanonicalStatus.DONE)
        if not ticket.is_status(done_name) and self._apply_transition(
            ticket, CanonicalStatus.DONE
        ):
            summary.cancelled_closed += 1
            log.info(
                "  Closed %s -- PR #%d cancelled (closed without merge)",
                ticket.key,
                pr.number,
            )

        # Dedup the honest note on its own key so a prior Review comment can't
        # suppress it (it's the only signal distinguishing a cancel from a merge).
        if self._state.record_cancel_commented(pr.url, ticket.key):
            self._jira.post_comment(
                ticket,
                pr,
                done_name,
                note="PR closed without merge -- ticket closed as cancelled.",
            )
            self._state.record_comment(
                pr.url,
                ticket.key,
                CanonicalStatus.DONE.value,
            )
            self._digest_event(
                "pr_linked",
                ticket_key=ticket.key,
                pr_url=pr.url,
                github_user=member.github,
                new_value=CanonicalStatus.DONE.value,
            )
            summary.commented += 1

    def _auto_create_for_unmatched_pr(
        self,
        pr: PullRequest,
        member: TeamMember,
        summary: SyncSummary,
    ) -> JiraTicket | None:
        """Ensure a Jira ticket exists for an unmatched PR.

        Returns the ticket to keep syncing, or None when nothing should happen:
        auto-create disabled, PR not open, shadow mode, or the PR's linked issues
        were tracked instead.
        """
        if not self._config.enable_auto_create or pr.state != "open":
            return None

        existing = self._jira.find_tracking_ticket(
            pr.url, self._config.jira_project_key
        )
        if existing:
            # A revived PR whose ticket is Done: in auto mode the resolver will
            # reopen it; in shadow mode just log it.
            if existing.is_status(self._status_name(CanonicalStatus.DONE)) and (
                self._config.stale_pr_close_mode == "shadow"
            ):
                log.info(
                    "  [SHADOW] Would reopen %s for PR #%d", existing.key, pr.number
                )
                return None
            return existing

        # A PR that closing-references issues: track the issue(s), as before.
        if pr.linked_issues:
            self._create_jira_from_linked_issues(pr, member, summary)
            return None
        if self._state.is_pr_tracked(pr.url):
            if self._state.record_pr_orphaned(pr.url):
                prior = self._state.get_tracked_ticket_key(pr.url) or "(unknown)"
                log.warning(
                    "  PR #%d previously tracked by %s but no Jira link found -- "
                    "ticket may have been closed or unlinked manually",
                    pr.number,
                    prior,
                )
                summary.pr_orphaned += 1
                self._digest_event(
                    "pr_orphaned",
                    ticket_key=prior,
                    pr_url=pr.url,
                    github_user=member.github,
                )
            return None

        description = ""
        if self._summarizer:
            description = self._summarizer.summarize(pr.title, pr.body)
        description = description or f"Upstream PR by @{member.github}."

        if self._config.claim_mode == "shadow":
            log.info(
                "  [SHADOW] Would create Jira for PR #%d: %s\n"
                "    Summary: %s\n"
                "    Assignee: %s\n"
                "    Project: %s",
                pr.number,
                pr.title[:60],
                description,
                member,
                self._config.jira_project_key,
            )
            summary.would_create += 1
            return None

        extra_fields = self._build_creation_fields(member, pr) or None
        ticket = self._jira.create_ticket(
            project_key=self._config.jira_project_key,
            summary=pr.title[:255],
            description_adf=AdfBuilder.pr_description(pr.title, pr.url, description),
            assignee_email=member.jira_email,
            extra_fields=extra_fields,
            issuetype=self._config.issue_type,
            components=self._config.jira_components or None,
            initial_status_name=self._status_name(CanonicalStatus.TODO),
        )
        self._state.record_pr_tracked(pr.url, ticket.key)

        # Add watchers to PR-tracked tickets
        if self._config.default_watchers:
            try:
                self._jira.add_watchers(ticket.key, self._config.default_watchers)
            except Exception:
                log.exception("  Failed to add watchers to %s", ticket.key)

        self._digest_event(
            "ticket_created",
            ticket_key=ticket.key,
            pr_url=pr.url,
            github_user=member.github,
        )
        summary.pr_tickets_created += 1
        log.info("  Created %s for PR #%d", ticket.key, pr.number)
        self._add_to_current_sprint(ticket, summary)
        return ticket

    def _close_stale_tickets(
        self,
        member: TeamMember,
        pr_reviews: list[PRWithReview],
        tickets: list[JiraTicket],
        summary: SyncSummary,
    ) -> None:
        """Close tickets whose linked PR(s) have had no activity for stale_pr_close_days."""
        days = self._config.stale_pr_close_days
        if days <= 0 or not tickets:
            return
        done_name = self._status_name(CanonicalStatus.DONE)
        active_pr_urls = {pwr.pr.url for pwr in pr_reviews}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        fetched: dict[str, PullRequest | None] = {}

        for ticket in tickets:
            if ticket.is_status(done_name):
                continue
            pr_urls = ticket.pr_links
            if not pr_urls or any(u in active_pr_urls for u in pr_urls):
                continue  # no linked PR, or one was active in this run
            for u in pr_urls:
                if u not in fetched:
                    fetched[u] = self._fetch_pr_for_url(u)
            prs = [fetched[u] for u in pr_urls]
            if not all(prs):
                continue  # couldn't verify a linked PR -- leave the ticket alone
            if any(pr.effectively_merged for pr in prs):
                continue  # a merged PR is the normal sync path's job, not this one
            if not all(self._is_pr_stale(pr, cutoff) for pr in prs):
                continue

            if self._config.stale_pr_close_mode == "shadow":
                log.info(
                    "  [SHADOW] Would close %s for %s -- linked PR(s) idle >%dd",
                    ticket.key,
                    member.github,
                    days,
                )
                continue

            if self._status_blocked_by_override(ticket, CanonicalStatus.DONE.value):
                continue

            try:
                if self._apply_transition(ticket, CanonicalStatus.DONE):
                    summary.stale_closed += 1
                    log.info(
                        "  Closed %s for %s -- linked PR(s) idle >%dd",
                        ticket.key,
                        member.github,
                        days,
                    )
                self._refresh_pr_state_snapshot(
                    ticket,
                    prs[0],
                    PRLifecycleState.CLOSED_UNMERGED.value,
                    CanonicalStatus.DONE.value,
                )
            except Exception:
                summary.errors += 1
                log.exception("  Failed to close stale ticket %s", ticket.key)

    def _fetch_pr_for_url(self, pr_url: str) -> PullRequest | None:
        try:
            number = pr_number_from_github_url(pr_url)
            if number is None:
                return None
            return self._github.get_pr(repo_from_github_url(pr_url), number)
        except Exception:
            log.exception("  Failed to fetch PR %s -- skipping stale check", pr_url)
            return None

    def _human_activity_expired(self, pr: PullRequest) -> bool:
        """True when the PR's last human activity predates the poll window, i.e. only
        bot activity pulled it back in. False when activity data is unavailable."""
        last = _parse_utc_timestamp(pr.last_human_activity_at)
        if last is None:
            return False
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self._config.poll_interval_hours
        )
        return last < cutoff

    @staticmethod
    def _is_pr_stale(pr: PullRequest, cutoff: datetime) -> bool:
        updated = _parse_utc_timestamp(pr.human_activity_at)
        return updated is not None and updated < cutoff

    def _close_superseded_claim_tickets(
        self,
        member: TeamMember,
        tickets: list[JiraTicket],
        summary: SyncSummary,
    ) -> None:
        """Retire a comment-claim ticket once another contributor opens a PR that will
        close the claimed issue. A claim ticket links only the issue (never a PR), so the
        stale-PR sweep never reaches it. Only not-yet-started tickets are auto-closed --
        in-progress / in-review work is left for a human. Gated by stale_pr_close_mode."""
        if not tickets:
            return
        done_name = self._status_name(CanonicalStatus.DONE)
        for ticket in tickets:
            if self._is_container(ticket) or ticket.is_status(done_name):
                continue
            if ticket.is_status(
                self._status_name(CanonicalStatus.IN_PROGRESS)
            ) or ticket.is_status(self._status_name(CanonicalStatus.REVIEW)):
                continue  # claimer is actively working -- leave it alone
            if not ticket.issue_links or ticket.pr_links:
                continue  # not a pure comment-claim ticket
            if not self._claim_superseded(ticket.issue_links, member.github):
                continue
            if self._config.stale_pr_close_mode == "shadow":
                log.info(
                    "  [SHADOW] Would close %s -- claimed issue taken by another contributor's PR",
                    ticket.key,
                )
                continue
            try:
                if self._apply_transition(ticket, CanonicalStatus.DONE):
                    summary.claim_superseded_closed += 1
                    log.info(
                        "  Closed %s for %s -- claimed issue superseded by another contributor's PR",
                        ticket.key,
                        member.github,
                    )
                    self._note_claim_superseded(ticket)
            except Exception:
                summary.errors += 1
                log.exception(
                    "  Failed to close superseded claim ticket %s", ticket.key
                )

    def _note_claim_superseded(self, ticket: JiraTicket) -> None:
        """Leave an honest close note. A note failure must not undo the close."""
        try:
            self._jira.post_note(
                ticket,
                "Auto-closed by the sync bot: another contributor opened a pull "
                "request that will resolve the claimed upstream issue, so this claim "
                "is no longer tracked. Reopen if you intend to continue the work.",
            )
        except Exception:
            log.exception("  Closed %s but failed to post the note", ticket.key)

    def _claim_superseded(self, issue_urls: list[str], claimer_login: str) -> bool:
        """True if any tracked issue has a closing PR by someone other than the claimer."""
        for url in issue_urls:
            try:
                repo = repo_from_github_url(url)
            except ValueError:
                continue
            number = issue_number_from_github_url(url)
            if number is None:
                continue
            if self._github.issue_has_competing_pr(repo, number, claimer_login):
                return True
        return False

    def _backfill_team_labels(
        self,
        member: TeamMember,
        tickets: list[JiraTicket],
        summary: SyncSummary,
    ) -> None:
        self._tagger.backfill_team_labels(member, tickets, summary)

    def _backfill_container_team_labels(
        self,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        """Team-tag the member's open container issues via a dedicated query, since they
        are kept out of the PR-sync candidate pool. RfcEpicTracker creates them team-less,
        so without this they never reach the team dashboard."""
        if not (self._team_classifier and self._config.enable_team_assignment):
            return
        containers = self._jira.get_open_containers(
            member.jira_email, self._config.jira_project_key
        )
        if containers:
            self._tagger.backfill_team_labels(member, containers, summary)

    def _sweep_sprint(
        self,
        member: TeamMember,
        summary: SyncSummary,
    ) -> None:
        self._tagger.sweep_sprint(member, summary)

    def _add_to_current_sprint(self, ticket: JiraTicket, summary: SyncSummary) -> None:
        """Add a freshly-created ticket to the current native sprint."""
        if not self._config.enable_sprint_tagging:
            return
        sprint = self._tagger.resolve_current_sprint()
        if sprint is None:
            return
        if self._config.sprint_mode == "shadow":
            log.info("  [SPRINT-SHADOW] Would add %s to %s", ticket.key, sprint.name)
            return
        try:
            self._jira.add_issues_to_sprint(sprint.id, [ticket.key])
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 400:
                log.warning("  Sprint add rejected (off-board?) %s: %s", ticket.key, e)
            else:
                log.warning(
                    "  Failed adding %s to sprint %s",
                    ticket.key,
                    sprint.name,
                    exc_info=True,
                )
                summary.errors += 1

"""SyncOrchestrator flows, TicketTagger, RFC epics, and the manual-override gate."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from conftest import (
    make_config,
    make_issue,
    make_linked_issue,
    make_pr,
    make_teams,
    make_ticket,
)
from upstream_jira_sync.jira import JiraClient
from upstream_jira_sync.models import (
    CanonicalStatus,
    ClaimResult,
    JiraTicket,
    MatchResult,
    PRWithReview,
    PullRequest,
    ReviewDecision,
    SprintRef,
    SyncSummary,
    TeamMember,
)
from upstream_jira_sync.orchestrator import SyncOrchestrator, _RFC_TITLE_RE
from upstream_jira_sync.override_gate import ManualOverrideGate
from upstream_jira_sync.sprint import (
    current_sprint_number,
    sprint_window,
    sprints_to_provision,
    sweep_cutoff_date,
)
from upstream_jira_sync.state import SyncState
from upstream_jira_sync.tagging import TicketTagger

MEMBER = TeamMember(github="octocat", jira_email="octocat@example.com")

CUSTOM_STATUS_MAP = {
    "todo": "New",
    "in_progress": "Doing",
    "review": "Code Review",
    "done": "Closed",
}


def _make_orchestrator(
    tmp_path,
    pr_reviews=None,
    tickets=None,
    match_result=None,
    target_status=CanonicalStatus.REVIEW,
    estimator=None,
    config=None,
    classifier=None,
    summarizer=None,
    deduplicator=None,
    override_gate=None,
    team_classifier=None,
    emailer=None,
):
    config = config or make_config()

    github = MagicMock()
    github.get_prs_by_user.return_value = pr_reviews or []
    github.get_issues.return_value = []

    jira = MagicMock()
    jira.get_open_tickets.return_value = tickets or []
    jira.transition_ticket.return_value = True
    jira.find_tracking_ticket.return_value = None

    matcher = MagicMock()
    matcher.find_best.return_value = match_result

    resolver = MagicMock()
    resolver.resolve.return_value = target_status

    state = SyncState(path=str(tmp_path / "state.json"))

    orch = SyncOrchestrator(
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
        override_gate=override_gate,
        team_classifier=team_classifier,
        emailer=emailer,
    )
    return orch, github, jira, matcher, state


def _http_error(status):
    err = RuntimeError(f"{status}")
    err.response = MagicMock()
    err.response.status_code = status
    return err


class TestSyncFlow:
    def test_full_sync_happy_path_uses_status_map_names(self, tmp_path):
        # R4: the orchestrator passes the instance's status NAME from status_map.
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test ticket", status="Doing")

        orch, _, jira, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="test"),
            config=make_config(status_map=CUSTOM_STATUS_MAP),
        )

        summary = orch.run()

        assert summary.matched == 1
        assert summary.transitioned == 1
        assert summary.commented == 1
        jira.transition_ticket.assert_called_once_with(ticket, "Code Review")
        assert jira.post_comment.call_args.args[2] == "Code Review"
        assert state.is_commented(pr.url, "PROJ-100")

    def test_matched_container_is_never_transitioned(self, tmp_path):
        # R5: the backstop respects the configured container type, not "Epic".
        pr = make_pr(number=42, state="closed", merged=True)
        container = make_ticket("PROJ-18", "Active RFC container")
        container.issuetype = "Theme"

        orch, _, jira, _, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[container],
            match_result=MatchResult(ticket=container, confidence="high", reason="t"),
            target_status=CanonicalStatus.DONE,
            config=make_config(container_issue_type="Theme"),
        )

        summary = orch.run()

        jira.transition_ticket.assert_not_called()
        assert summary.transitioned == 0

    def test_draft_pr_skipped(self, tmp_path):
        orch, _, _, matcher, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(make_pr(draft=True), ReviewDecision.NONE, 0)],
            tickets=[make_ticket("PROJ-100", "Test")],
        )

        summary = orch.run()
        assert summary.draft == 1
        assert summary.seen == 1
        matcher.find_best.assert_not_called()

    def test_open_ignored_label_pr_skipped(self, tmp_path):
        # Lowercase label proves the match is case-insensitive against config "Stale".
        pr = make_pr(state="open", labels=("stale",))
        orch, _, jira, matcher, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[make_ticket("PROJ-100", "Test")],
            config=make_config(ignore_pr_labels=["Stale"]),
        )

        summary = orch.run()
        assert summary.ignored_label == 1
        matcher.find_best.assert_not_called()
        jira.transition_ticket.assert_not_called()

    def test_terminal_ignored_label_pr_flows_through(self, tmp_path):
        pr = make_pr(state="closed", merged=True, labels=("Stale",))
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, jira, matcher, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
            target_status=CanonicalStatus.DONE,
            config=make_config(ignore_pr_labels=["Stale"]),
        )

        summary = orch.run()
        assert summary.ignored_label == 0
        matcher.find_best.assert_called_once()
        jira.transition_ticket.assert_called_once()

    def test_no_match_increments_low_conf(self, tmp_path):
        orch, _, _, _, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(make_pr(), ReviewDecision.NONE, 0)],
            tickets=[make_ticket("PROJ-100", "Test")],
            match_result=None,
        )

        summary = orch.run()
        assert summary.low_conf == 1
        assert summary.matched == 0

    def test_duplicate_comment_skipped_via_state(self, tmp_path):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, jira, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
        )
        state.record_comment(pr.url, "PROJ-100", "review")

        summary = orch.run()
        assert summary.commented == 0
        assert summary.comment_dedup == 1
        jira.post_comment.assert_not_called()

    def test_previously_linked_pr_skips_matcher(self, tmp_path):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, _, matcher, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket, make_ticket("PROJ-101", "Other")],
            match_result=None,
        )
        state.record_comment(pr.url, "PROJ-100", "review")

        summary = orch.run()
        matcher.find_best.assert_not_called()
        assert summary.matched == 1
        assert summary.low_conf == 0

    def test_linked_ticket_missing_from_candidates_falls_through_to_matcher(
        self, tmp_path
    ):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, _, matcher, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
        )
        state.record_comment(pr.url, "PROJ-999", "review")  # closed / reassigned

        orch.run()
        matcher.find_best.assert_called_once()

    def test_unchanged_low_conf_pr_skips_matcher_until_human_activity(self, tmp_path):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test")
        orch, github, _, matcher, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=None,
        )
        # Closed PR: no auto-create, so the low-confidence verdict is remembered.
        pr = replace(pr, state="closed")
        github.get_prs_by_user.return_value = [PRWithReview(pr, ReviewDecision.NONE, 0)]

        orch.run()
        assert matcher.find_best.call_count == 1
        assert state.is_low_conf_unchanged(pr.url, pr.human_activity_at)

        orch.run()
        assert matcher.find_best.call_count == 1  # unchanged PR not re-sent

        bumped = replace(
            pr,
            last_human_activity_at=(
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
        )
        github.get_prs_by_user.return_value = [
            PRWithReview(bumped, ReviewDecision.NONE, 0)
        ]
        orch.run()
        assert matcher.find_best.call_count == 2

    def test_match_clears_low_conf_memory(self, tmp_path):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, _, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
        )
        # Verdict from before the PR was last touched: matcher runs and matches.
        state.record_low_conf_match(pr.url, "2026-01-01T00:00:00Z")

        orch.run()
        assert pr.url not in state._data["low_conf_matches"]

    def test_no_tickets_skips_member(self, tmp_path):
        orch, _, _, matcher, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(make_pr(), ReviewDecision.NONE, 0)],
            tickets=[],
        )
        orch.run()
        matcher.find_best.assert_not_called()

    def test_error_increments_error_count(self, tmp_path):
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, jira, _, _ = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(make_pr(), ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
        )
        jira.transition_ticket.side_effect = Exception("API down")

        assert orch.run().errors == 1


class TestCancelledPRFlow:
    def _orch(self, tmp_path, pr, mode="auto", **config_overrides):
        ticket = make_ticket("PROJ-100", "Test")
        orch, github, jira, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
            target_status=CanonicalStatus.DONE,
            config=make_config(stale_pr_close_mode=mode, **config_overrides),
        )
        return orch, github, jira, state, ticket

    def test_merge_label_pr_closes_immediately_not_as_cancel(self, tmp_path):
        pr = make_pr(
            state="closed", merged=False, labels=("Merged",), merge_labels=("Merged",)
        )
        orch, _, jira, _, ticket = self._orch(tmp_path, pr, merge_labels=["Merged"])

        orch.run()
        jira.transition_ticket.assert_called_once_with(ticket, "Done")
        _, kwargs = jira.post_comment.call_args
        assert kwargs.get("note", "") == ""

    def test_cancelled_pr_closes_on_second_run_with_note(self, tmp_path):
        pr = make_pr(state="closed", merged=False)
        orch, _, jira, _, ticket = self._orch(tmp_path, pr)

        orch.run()  # debounce
        summary = orch.run()  # confirm + close

        jira.transition_ticket.assert_called_once_with(ticket, "Done")
        assert summary.cancelled_closed == 1
        _, kwargs = jira.post_comment.call_args
        assert "closed without merge" in kwargs["note"].lower()

    def test_cancel_note_posts_despite_prior_open_comment(self, tmp_path):
        pr_open = make_pr(state="open")
        orch, github, jira, _, _ = self._orch(tmp_path, pr_open)
        orch._resolver.resolve.return_value = CanonicalStatus.REVIEW

        orch.run()  # open -> Review comment recorded under pr_url::ticket_key
        github.get_prs_by_user.return_value = [
            PRWithReview(make_pr(state="closed", merged=False), ReviewDecision.NONE, 0)
        ]
        orch._resolver.resolve.return_value = CanonicalStatus.DONE
        orch.run()  # first cancel observation -> debounce
        orch.run()  # confirm -> close + honest note

        notes = [c.kwargs.get("note", "") for c in jira.post_comment.call_args_list]
        assert any("closed without merge" in n.lower() for n in notes)

    def test_cancelled_pr_shadow_mode_does_not_close(self, tmp_path):
        pr = make_pr(state="closed", merged=False)
        orch, _, jira, _, _ = self._orch(tmp_path, pr, mode="shadow")

        orch.run()
        orch.run()
        jira.transition_ticket.assert_not_called()
        jira.post_comment.assert_not_called()


class TestEstimation:
    def _orch(self, tmp_path, estimate_value=5):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test ticket")
        estimator = MagicMock()
        estimator.estimate.return_value = estimate_value
        orch, _, jira, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
            estimator=estimator,
            config=make_config(
                enable_estimation=True, story_points_field="customfield_12345"
            ),
        )
        return orch, jira, estimator, state, pr, ticket

    def test_estimation_called_and_points_set_on_configured_field(self, tmp_path):
        orch, jira, estimator, state, pr, ticket = self._orch(tmp_path)
        summary = orch.run()

        estimator.estimate.assert_called_once()
        jira.set_story_points.assert_called_once_with(ticket, 5, "customfield_12345")
        assert summary.estimated == 1
        assert state.is_estimated(pr.url, "PROJ-100")

    def test_estimation_skipped_if_already_done(self, tmp_path):
        orch, jira, estimator, state, pr, _ = self._orch(tmp_path)
        state.record_estimation(pr.url, "PROJ-100", 5)

        orch.run()
        estimator.estimate.assert_not_called()
        jira.set_story_points.assert_not_called()

    def test_estimation_none_does_not_write(self, tmp_path):
        orch, jira, estimator, _, _, _ = self._orch(tmp_path, estimate_value=None)

        summary = orch.run()
        estimator.estimate.assert_called_once()
        jira.set_story_points.assert_not_called()
        assert summary.estimated == 0

    def test_estimation_skipped_for_container(self, tmp_path):
        orch, jira, estimator, _, _, _ = self._orch(tmp_path)
        container = make_ticket("PROJ-11", "Umbrella work")
        container.issuetype = "Epic"

        orch._try_estimate(make_pr(number=7), container, MEMBER, SyncSummary())
        orch._try_estimate_from_issue(container, "title", "body", SyncSummary())

        estimator.estimate.assert_not_called()
        estimator.estimate_from_issue.assert_not_called()
        jira.set_story_points.assert_not_called()


class TestIssueClaiming:
    def _orch(
        self, tmp_path, claim_intent="claiming", claim_mode="shadow", **overrides
    ):
        config = make_config(
            enable_auto_create=True,
            claim_mode=claim_mode,
            issue_type="Task",
            **overrides,
        )
        classifier = MagicMock()
        classifier.classify.return_value = ClaimResult(intent=claim_intent, reason="t")
        orch, github, jira, _, state = _make_orchestrator(
            tmp_path, config=config, classifier=classifier
        )
        github.get_issues.return_value = [make_issue()]
        jira.create_ticket.return_value = make_ticket(
            "PROJ-NEW", "Test", status="To Do"
        )
        return orch, github, jira, classifier, state

    def test_shadow_mode_logs_but_does_not_create(self, tmp_path):
        orch, _, jira, clf, state = self._orch(tmp_path, claim_mode="shadow")
        summary = orch.run()

        clf.classify.assert_called_once()
        jira.create_ticket.assert_not_called()
        assert summary.issues_created == 0
        assert summary.would_create == 1
        assert state.is_issue_processed(make_issue().url)

    def test_auto_mode_creates_ticket_with_configured_issue_type(self, tmp_path):
        orch, _, jira, _, _ = self._orch(tmp_path, claim_mode="auto")
        summary = orch.run()

        jira.create_ticket.assert_called_once()
        kwargs = jira.create_ticket.call_args.kwargs
        assert kwargs["issuetype"] == "Task"  # R5: config.issue_type, not "Story"
        assert kwargs["initial_status_name"] == "To Do"
        assert kwargs["extra_fields"] is None
        assert summary.issues_created == 1

    def test_not_claiming_skips_creation(self, tmp_path):
        orch, _, jira, _, _ = self._orch(
            tmp_path, claim_intent="not_claiming", claim_mode="auto"
        )
        assert orch.run().issues_created == 0
        jira.create_ticket.assert_not_called()

    def test_existing_jira_skips_creation(self, tmp_path):
        orch, _, jira, _, _ = self._orch(tmp_path, claim_mode="auto")
        jira.find_tracking_ticket.return_value = make_ticket("PROJ-EXISTS", "Tracked")
        orch.run()
        jira.create_ticket.assert_not_called()

    def test_already_processed_issue_skipped(self, tmp_path):
        orch, _, _, clf, state = self._orch(tmp_path, claim_mode="auto")
        state.record_issue_classification(make_issue().url, "claiming", "prev")
        orch.run()
        clf.classify.assert_not_called()

    def test_get_issues_called_with_is_open(self, tmp_path):
        orch, github, _, _, _ = self._orch(tmp_path, claim_mode="auto")
        orch.run()
        assert github.get_issues.call_args.kwargs.get("is_open") is True

    def test_closed_issue_skips_classifier_and_creation(self, tmp_path):
        orch, _, jira, clf, state = self._orch(tmp_path, claim_mode="auto")
        closed_issue = replace(make_issue(number=99), state="closed")

        orch._classify_and_handle_issue(closed_issue, MEMBER, MagicMock())

        clf.classify.assert_not_called()
        jira.create_ticket.assert_not_called()
        assert not state.is_issue_processed(closed_issue.url)

    def test_disabled_auto_create_skips_issues(self, tmp_path):
        orch, github, _, _, _ = _make_orchestrator(
            tmp_path,
            config=make_config(enable_auto_create=False),
            classifier=MagicMock(),
        )
        orch.run()
        github.get_issues.assert_not_called()

    def test_claim_time_estimate_sets_points_on_new_ticket(self, tmp_path):
        estimator = MagicMock()
        estimator.estimate_from_issue.return_value = 3
        orch, _, jira, _, _ = self._orch(
            tmp_path,
            claim_mode="auto",
            enable_estimation=True,
            story_points_field="customfield_12345",
        )
        orch._estimator = estimator

        summary = orch.run()

        estimator.estimate_from_issue.assert_called_once()
        jira.set_story_points.assert_called_once_with(
            jira.create_ticket.return_value, 3, "customfield_12345"
        )
        assert summary.issues_created == 1
        assert summary.estimated == 1


class TestPRLinkedIssueCreation:
    def _orch(self, tmp_path, claim_mode="shadow", linked_issues=(), deduplicator=None):
        pr = make_pr(
            number=42, title="Fix overflow", linked_issues=tuple(linked_issues)
        )
        orch, github, jira, matcher, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            match_result=None,
            deduplicator=deduplicator,
            config=make_config(enable_auto_create=True, claim_mode=claim_mode),
        )
        jira.create_ticket.return_value = make_ticket(
            "PROJ-NEW", "Test", status="To Do"
        )
        return orch, jira, state, pr

    def test_ai_dedup_skips_duplicate_creation(self, tmp_path):
        dedup = MagicMock()
        dedup.find_existing.return_value = MatchResult(
            ticket=make_ticket("PROJ-13", "Already tracked"),
            confidence="high",
            reason="same",
        )
        orch, jira, _, _ = self._orch(
            tmp_path,
            claim_mode="auto",
            linked_issues=[make_linked_issue()],
            deduplicator=dedup,
        )
        orch.run()
        dedup.find_existing.assert_called_once()
        jira.create_ticket.assert_not_called()

    def test_auto_mode_creates_ticket_from_linked_issue(self, tmp_path):
        linked = make_linked_issue()
        orch, jira, state, _ = self._orch(
            tmp_path, claim_mode="auto", linked_issues=[linked]
        )
        summary = orch.run()

        jira.create_ticket.assert_called_once()
        assert summary.issues_created == 1
        assert state.is_issue_processed(linked.url)

    def test_shadow_mode_logs_linked_issue(self, tmp_path):
        linked = make_linked_issue()
        orch, jira, state, _ = self._orch(
            tmp_path, claim_mode="shadow", linked_issues=[linked]
        )
        summary = orch.run()

        jira.create_ticket.assert_not_called()
        assert summary.issues_created == 0
        assert state.is_issue_processed(linked.url)

    def test_not_claiming_verdict_superseded_by_linked_pr(self, tmp_path):
        linked = make_linked_issue()
        orch, jira, state, _ = self._orch(
            tmp_path, claim_mode="auto", linked_issues=[linked]
        )
        state.record_issue_classification(
            linked.url, "not_claiming", "Comment was analysis, not a claim."
        )
        summary = orch.run()

        jira.create_ticket.assert_called_once()
        assert summary.issues_created == 1
        assert not state.is_issue_not_claiming(linked.url)

    def test_ticketed_classification_still_dedups_linked_pr(self, tmp_path):
        linked = make_linked_issue()
        orch, jira, state, _ = self._orch(
            tmp_path, claim_mode="auto", linked_issues=[linked]
        )
        state.record_issue_classification(
            linked.url, "pr_linked", "Referenced by PR #41"
        )
        state.set_issue_ticket(linked.url, "PROJ-7")
        summary = orch.run()

        jira.create_ticket.assert_not_called()
        assert summary.issues_created == 0


class TestPRTicketCreation:
    def _orch(self, tmp_path, pr, stale_pr_close_mode="auto"):
        orch, github, jira, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            match_result=None,
            config=make_config(
                enable_auto_create=True,
                claim_mode="auto",
                stale_pr_close_mode=stale_pr_close_mode,
            ),
        )
        jira.create_ticket.return_value = make_ticket(
            "PROJ-NEW", "Auto", status="To Do"
        )
        return orch, jira, state

    def test_open_pr_no_match_creates_and_syncs(self, tmp_path):
        pr = make_pr(number=77)
        orch, jira, state = self._orch(tmp_path, pr)
        summary = orch.run()

        jira.create_ticket.assert_called_once()
        assert (
            jira.create_ticket.call_args.kwargs["assignee_email"]
            == "octocat@example.com"
        )
        assert summary.pr_tickets_created == 1
        assert summary.transitioned == 1
        assert summary.commented == 1
        assert state.is_pr_tracked(pr.url)

    def test_closed_pr_does_not_create(self, tmp_path):
        orch, jira, _ = self._orch(tmp_path, make_pr(number=77, state="closed"))
        orch.run()
        jira.create_ticket.assert_not_called()

    def test_existing_closed_ticket_reopened(self, tmp_path):
        closed = make_ticket("PROJ-OLD", "Was closed", status="Done")
        orch, jira, _ = self._orch(tmp_path, make_pr(number=77))
        jira.find_tracking_ticket.return_value = closed
        orch.run()
        jira.create_ticket.assert_not_called()
        jira.transition_ticket.assert_called_once_with(closed, "In Review")

    def test_bot_bumped_pr_does_not_reopen_closed_ticket(self, tmp_path):
        closed = make_ticket("PROJ-OLD", "Was closed", status="Done")
        pr = make_pr(
            number=77,
            last_human_activity_at=(
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat(),
        )
        orch, jira, _ = self._orch(tmp_path, pr)
        jira.find_tracking_ticket.return_value = closed
        summary = orch.run()
        jira.transition_ticket.assert_not_called()
        assert summary.bot_activity == 1

    def test_closed_ticket_not_reopened_in_shadow_mode(self, tmp_path):
        closed = make_ticket("PROJ-OLD", "Was closed", status="Done")
        orch, jira, _ = self._orch(
            tmp_path, make_pr(number=77), stale_pr_close_mode="shadow"
        )
        jira.find_tracking_ticket.return_value = closed
        orch.run()
        jira.transition_ticket.assert_not_called()


class TestCoAuthorCredit:
    def _orch(self, tmp_path, **config_overrides):
        ticket = make_ticket("PROJ-100", "Test ticket")
        config = make_config(
            team=[
                TeamMember(github="octocat", jira_email="octocat@example.com"),
                TeamMember(github="coauthor", jira_email="co@example.com"),
            ],
            **config_overrides,
        )
        pr = make_pr(
            number=42, author="octocat", commit_authors=("octocat", "coauthor")
        )
        orch, _, jira, _, state = _make_orchestrator(
            tmp_path,
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="test"),
            config=config,
        )
        return orch, jira, state, config, pr, ticket

    def test_note_posted_once(self, tmp_path):
        orch, jira, state, config, pr, ticket = self._orch(tmp_path)
        jira.resolve_account_id.return_value = "acct-co"
        summary = SyncSummary()
        pr_review = PRWithReview(pr, ReviewDecision.NONE, 0)

        orch._process_pr(pr_review, [ticket], config.team[0], summary)
        orch._process_pr(pr_review, [ticket], config.team[0], summary)

        jira.post_mention_note.assert_called_once()
        _, before, account_id, after = jira.post_mention_note.call_args.args
        assert "Multi-author PR" in before
        assert account_id == "acct-co"
        assert state.is_co_contribution_noted(pr.url, "PROJ-100", "coauthor")
        assert summary.co_authors_noted == 1

    def test_contributor_field_written_when_configured(self, tmp_path):
        orch, jira, _, config, pr, ticket = self._orch(
            tmp_path, contributors_field="customfield_10999"
        )
        jira.resolve_account_id.return_value = "acct-co"

        orch._process_pr(
            PRWithReview(pr, ReviewDecision.NONE, 0),
            [ticket],
            config.team[0],
            SyncSummary(),
        )

        jira.resolve_account_id.assert_called_once_with("co@example.com")
        jira.add_contributor.assert_called_once_with(
            "PROJ-100", "acct-co", "customfield_10999"
        )


class TestCloseStaleTickets:
    PR_URL = "https://github.com/exampleorg/widgets/pull/123"

    def _run(self, tmp_path, mode, pr=None, issuetype=""):
        idle_pr = pr or PullRequest(
            number=123,
            title="t",
            url=self.PR_URL,
            state="open",
            merged=False,
            draft=False,
            updated_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
        )
        ticket = JiraTicket(
            key="PROJ-1",
            summary="s",
            status="In Review",
            url="u",
            remote_links=[self.PR_URL],
            issuetype=issuetype,
        )
        orch, github, jira, _, _ = _make_orchestrator(
            tmp_path,
            tickets=[ticket],
            config=make_config(stale_pr_close_days=21, stale_pr_close_mode=mode),
        )
        github.get_pr.return_value = idle_pr
        summary = orch.run()
        return summary, jira, ticket

    def test_auto_mode_closes_idle_ticket(self, tmp_path):
        summary, jira, ticket = self._run(tmp_path, "auto")
        jira.transition_ticket.assert_called_once_with(ticket, "Done")
        assert summary.stale_closed == 1

    def test_shadow_mode_does_not_close(self, tmp_path):
        summary, jira, _ = self._run(tmp_path, "shadow")
        jira.transition_ticket.assert_not_called()
        assert summary.stale_closed == 0

    def test_bot_activity_does_not_reset_staleness_clock(self, tmp_path):
        bot_bumped = PullRequest(
            number=123,
            title="t",
            url=self.PR_URL,
            state="open",
            merged=False,
            draft=False,
            updated_at=datetime.now(timezone.utc).isoformat(),
            last_human_activity_at=(
                datetime.now(timezone.utc) - timedelta(days=40)
            ).isoformat(),
        )
        summary, jira, ticket = self._run(tmp_path, "auto", pr=bot_bumped)
        jira.transition_ticket.assert_called_once_with(ticket, "Done")
        assert summary.stale_closed == 1

    def test_sweep_defers_merge_label_pr(self, tmp_path):
        # A merge-labeled close is effectively merged (R6): normal close path's job.
        merged_pr = PullRequest(
            number=123,
            title="t",
            url=self.PR_URL,
            state="closed",
            merged=False,
            draft=False,
            updated_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat(),
            labels=("Merged",),
            merge_labels=("Merged",),
        )
        summary, jira, _ = self._run(tmp_path, "auto", pr=merged_pr)
        jira.transition_ticket.assert_not_called()
        assert summary.stale_closed == 0

    def test_stale_container_is_not_closed(self, tmp_path):
        summary, jira, _ = self._run(tmp_path, "auto", issuetype="Epic")
        jira.transition_ticket.assert_not_called()
        assert summary.stale_closed == 0


class TestCloseSupersededClaimTickets:
    ISSUE_URL = "https://github.com/exampleorg/widgets/issues/88"
    PR_URL = "https://github.com/exampleorg/widgets/pull/91"

    def _orch(self, tmp_path, mode, competing):
        orch, github, jira, _, _ = _make_orchestrator(
            tmp_path, config=make_config(stale_pr_close_mode=mode)
        )
        github.issue_has_competing_pr.return_value = competing
        return orch, github, jira

    def _ticket(self, status="To Do", links=None):
        return JiraTicket(
            key="PROJ-1",
            summary="claim",
            status=status,
            url="u",
            remote_links=[self.ISSUE_URL] if links is None else links,
        )

    def test_closes_claim_when_competing_pr(self, tmp_path):
        orch, github, jira = self._orch(tmp_path, "auto", True)
        ticket = self._ticket()
        summary = SyncSummary()
        orch._close_superseded_claim_tickets(MEMBER, [ticket], summary)

        github.issue_has_competing_pr.assert_called_once_with(
            "exampleorg/widgets", 88, "octocat"
        )
        jira.transition_ticket.assert_called_once_with(ticket, "Done")
        jira.post_note.assert_called_once()
        assert summary.claim_superseded_closed == 1

    def test_shadow_does_not_close(self, tmp_path):
        orch, _, jira = self._orch(tmp_path, "shadow", True)
        orch._close_superseded_claim_tickets(MEMBER, [self._ticket()], SyncSummary())
        jira.transition_ticket.assert_not_called()
        jira.post_note.assert_not_called()

    def test_protections(self, tmp_path):
        # No competing PR -> not closed.
        orch, _, jira = self._orch(tmp_path, "auto", False)
        orch._close_superseded_claim_tickets(MEMBER, [self._ticket()], SyncSummary())
        jira.transition_ticket.assert_not_called()

        # A ticket linking a PR is not a pure claim ticket: never even checked.
        orch2, github2, jira2 = self._orch(tmp_path, "auto", True)
        orch2._close_superseded_claim_tickets(
            MEMBER, [self._ticket(links=[self.ISSUE_URL, self.PR_URL])], SyncSummary()
        )
        github2.issue_has_competing_pr.assert_not_called()
        jira2.transition_ticket.assert_not_called()

        # An in-progress claim means the claimer is working: leave it alone.
        orch3, _, jira3 = self._orch(tmp_path, "auto", True)
        orch3._close_superseded_claim_tickets(
            MEMBER, [self._ticket(status="In Progress")], SyncSummary()
        )
        jira3.transition_ticket.assert_not_called()


class TestRfcEpics:
    RFC = "https://github.com/exampleorg/widgets/issues/777"
    RFC2 = "https://github.com/exampleorg/widgets/issues/888"

    def _orch(self, tmp_path, state_file="s.json", **config_overrides):
        config = make_config(
            enable_auto_create=True,
            enable_rfc_epics=True,
            rfc_epic_mode="auto",
            **config_overrides,
        )
        orch, _, jira, _, state = _make_orchestrator(
            tmp_path, config=config, classifier=MagicMock()
        )
        orch._state = state = SyncState(path=str(tmp_path / state_file))
        orch._rfc_epics._state = state
        jira.find_epic_for_rfc.return_value = None
        jira.create_ticket.return_value = make_ticket("PROJ-EPIC", "[RFC] Foo")
        return orch, jira, state

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("[RFC] Foo", True),
            ("RFC: Bar", True),
            ("rfc fix xyz", False),
            ("Implement RFC engine", False),
        ],
    )
    def test_rfc_title_detection(self, title, expected):
        assert bool(_RFC_TITLE_RE.match(title)) is expected

    def test_classifier_confirms_epic_path_uses_container_type(self, tmp_path):
        orch, jira, _ = self._orch(tmp_path, container_issue_type="Initiative")
        orch._rfc_classifier = MagicMock()
        orch._rfc_classifier.classify.return_value = "epic"
        issue = make_issue(777, title="[RFC] Sharded checkpoints")

        orch._classify_and_handle_issue(issue, MEMBER, MagicMock())

        kwargs = jira.create_ticket.call_args.kwargs
        assert kwargs["issuetype"] == "Initiative"  # R5
        assert kwargs["extra_fields"] is None  # R8: no epic_name_field configured
        orch._classifier.classify.assert_not_called()

    def test_epic_name_field_included_only_when_configured(self, tmp_path):
        orch, jira, state = self._orch(tmp_path, epic_name_field="customfield_12311")

        key = orch._ensure_epic_for_rfc(self.RFC, "[RFC] Foo", "body", MEMBER)

        assert key == "PROJ-EPIC"
        kwargs = jira.create_ticket.call_args.kwargs
        assert kwargs["extra_fields"] == {"customfield_12311": "[RFC] Foo"}
        link_kwargs = jira.add_remote_link.call_args.kwargs
        assert link_kwargs["global_id"] == "rfc-epic::" + self.RFC

    def test_epic_created_with_recovery_order(self, tmp_path):
        orch, jira, state = self._orch(tmp_path)
        state_at_link_time = []

        def fail_link(*args, **kwargs):
            state_at_link_time.append(state.get_rfc_epic(self.RFC))
            raise RuntimeError("link write failed")

        jira.add_remote_link.side_effect = fail_link

        key = orch._ensure_epic_for_rfc(self.RFC, "[RFC] Foo", "body", MEMBER)

        assert key == "PROJ-EPIC"
        assert state_at_link_time == ["PROJ-EPIC"]
        assert state.get_rfc_epic(self.RFC) == "PROJ-EPIC"

    def test_classifier_story_verdict_falls_through_to_claim_flow(self, tmp_path):
        orch, jira, _ = self._orch(tmp_path)
        orch._rfc_classifier = MagicMock()
        orch._rfc_classifier.classify.return_value = "story"
        orch._classifier.classify.return_value = ClaimResult(
            intent="not_claiming", reason="question"
        )
        issue = make_issue(778, title="RFC: question about wheels")

        orch._classify_and_handle_issue(issue, MEMBER, MagicMock())

        jira.create_ticket.assert_not_called()
        orch._classifier.classify.assert_called_once()

    def test_classifier_error_skips_and_retries(self, tmp_path):
        orch, jira, state = self._orch(tmp_path)
        orch._rfc_classifier = MagicMock()
        orch._rfc_classifier.classify.return_value = None
        issue = make_issue(779, title="[RFC] Flaky check")

        orch._classify_and_handle_issue(issue, MEMBER, MagicMock())

        jira.create_ticket.assert_not_called()
        assert not state.is_issue_processed(issue.url)

    def test_ensure_epic_idempotent(self, tmp_path):
        orch, jira, state = self._orch(tmp_path)
        state.record_rfc_epic(self.RFC, "PROJ-1")
        assert (
            orch._ensure_epic_for_rfc(self.RFC, "[RFC] Foo", "body", MEMBER) == "PROJ-1"
        )
        jira.find_epic_for_rfc.assert_not_called()
        jira.create_ticket.assert_not_called()

        orch2, jira2, state2 = self._orch(tmp_path, state_file="s2.json")
        jira2.find_epic_for_rfc.return_value = make_ticket("PROJ-2", "[RFC] Foo")
        assert (
            orch2._ensure_epic_for_rfc(self.RFC, "[RFC] Foo", "body", MEMBER)
            == "PROJ-2"
        )
        assert state2.get_rfc_epic(self.RFC) == "PROJ-2"
        jira2.create_ticket.assert_not_called()

    @staticmethod
    def _rfc_issue(url):
        from upstream_jira_sync.models import LinkedIssue

        return LinkedIssue(
            number=int(url.rsplit("/", 1)[1]), title="[RFC] Foo", url=url, body="b"
        )

    def test_creation_parents_single_rfc(self, tmp_path):
        orch, _, state = self._orch(tmp_path)
        state.record_rfc_epic(self.RFC, "PROJ-EPIC")
        pr = replace(make_pr(), linked_issues=(self._rfc_issue(self.RFC),))
        fields = orch._build_creation_fields(MEMBER, pr)
        assert fields["parent"] == {"key": "PROJ-EPIC"}

    def test_multi_rfc_skips_with_warning(self, tmp_path, caplog):
        orch, jira, _ = self._orch(tmp_path)
        pr = replace(
            make_pr(),
            linked_issues=(self._rfc_issue(self.RFC), self._rfc_issue(self.RFC2)),
        )
        with caplog.at_level("WARNING"):
            assert orch._rfc_parent_for_pr(pr, MEMBER) is None
        assert any("multiple RFCs" in r.message for r in caplog.records)
        jira.find_epic_for_rfc.assert_not_called()

    def test_posthoc_reparent_fail_closed(self, tmp_path):
        orch, jira, state = self._orch(tmp_path)
        state.record_rfc_epic(self.RFC, "PROJ-EPIC")
        gate = MagicMock()
        gate.is_unreliable.return_value = True
        orch._override_gate = gate
        pr = replace(make_pr(), linked_issues=(self._rfc_issue(self.RFC),))
        orch._rfc_epics.maybe_reparent(
            pr, make_ticket("PROJ-5", "Story"), MEMBER, override_gate=gate
        )
        jira.set_parent.assert_not_called()


class TestLowConfEmailPing:
    def _orch(self, tmp_path, mode="auto", emailer=None):
        orch, _, _, _, state = _make_orchestrator(
            tmp_path,
            config=make_config(enable_low_conf_email=True, low_conf_email_mode=mode),
            emailer=emailer,
        )
        return orch, state

    def test_pings_once_per_pr(self, tmp_path):
        emailer = MagicMock()
        orch, state = self._orch(tmp_path, emailer=emailer)
        pr = make_pr()

        orch._maybe_ping_low_confidence(pr, MEMBER)
        orch._maybe_ping_low_confidence(pr, MEMBER)

        emailer.send.assert_called_once()
        assert emailer.send.call_args.args[0] == "octocat@example.com"
        assert state.is_low_conf_pinged(pr.url)

    def test_shadow_logs_without_sending(self, tmp_path, caplog):
        emailer = MagicMock()
        orch, state = self._orch(tmp_path, mode="shadow", emailer=emailer)

        with caplog.at_level("INFO"):
            orch._maybe_ping_low_confidence(make_pr(), MEMBER)

        emailer.send.assert_not_called()
        assert any("EMAIL-SHADOW" in r.message for r in caplog.records)
        assert not state.is_low_conf_pinged(make_pr().url)


class TestDigestGating:
    """Digest events are recorded only when digest is enabled (R11)."""

    def _run(self, tmp_path, digest_enabled):
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test")
        orch, _, _, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
            config=make_config(
                digest_enabled=digest_enabled, digest_repo="exampleorg/widgets"
            ),
        )
        orch.run()
        return state.read_digest_events("1970-01-01T00:00:00+00:00")

    def test_disabled_digest_records_nothing(self, tmp_path):
        assert self._run(tmp_path, digest_enabled=False) == []

    def test_enabled_digest_records_attributed_pr_linked_event(self, tmp_path):
        events = self._run(tmp_path, digest_enabled=True)
        assert len(events) == 1
        assert events[0]["kind"] == "pr_linked"
        assert events[0]["github_user"] == "octocat"
        assert events[0]["ticket_key"] == "PROJ-100"


def _team_config(mode="auto", **overrides):
    return make_config(
        enable_team_assignment=True,
        team_assignment_mode=mode,
        teams=make_teams(),
        team_field="customfield_11111",
        **overrides,
    )


class TestTeamAssignment:
    def _orch(self, tmp_path, mode, classifier, pr=None):
        orch, github, jira, _, _ = _make_orchestrator(
            tmp_path, config=_team_config(mode), team_classifier=classifier
        )
        github.get_pr_diff.return_value = "diff --git a/net/x.py b/net/x.py\n"
        if pr is not None:
            github.get_pr.return_value = pr
        return orch, github, jira

    def _classifier(self, teams):
        classifier = MagicMock()
        classifier.classify_ordered.return_value = teams
        return classifier

    def test_creation_adds_team_label_and_field_in_auto(self, tmp_path):
        orch, _, _ = self._orch(tmp_path, "auto", self._classifier(["Team Alpha"]))
        assert orch._build_creation_fields(MEMBER, make_pr()) == {
            "labels": ["team-alpha"],
            "customfield_11111": "team-uuid-alpha",
        }

    def test_creation_skips_in_shadow(self, tmp_path, caplog):
        orch, _, _ = self._orch(tmp_path, "shadow", self._classifier(["Team Alpha"]))
        with caplog.at_level("INFO", logger="upstream_jira_sync.orchestrator"):
            fields = orch._build_creation_fields(MEMBER, make_pr())
        assert fields == {}
        assert any("[TEAM-SHADOW]" in r.message for r in caplog.records)

    def test_backfill_writes_in_auto(self, tmp_path):
        pr = make_pr(number=42)
        orch, _, jira = self._orch(
            tmp_path, "auto", self._classifier(["Team Alpha"]), pr=pr
        )
        ticket = make_ticket("PROJ-1", "Existing")
        ticket.labels = ["sprint-2026-06"]
        ticket.remote_links = ["https://github.com/exampleorg/widgets/pull/42"]

        orch._backfill_team_labels(MEMBER, [ticket], SyncSummary())

        jira.update_labels.assert_called_once_with(
            "PROJ-1", ["sprint-2026-06", "team-alpha"]
        )
        jira.set_team.assert_called_once_with(
            "PROJ-1", "team-uuid-alpha", "customfield_11111"
        )

    def test_backfill_skips_when_team_already_set(self, tmp_path):
        orch, _, jira = self._orch(tmp_path, "auto", self._classifier(["Team Alpha"]))
        ticket = make_ticket("PROJ-1", "Existing")
        ticket.team_id = "team-uuid-beta"
        orch._backfill_team_labels(MEMBER, [ticket], SyncSummary())
        jira.update_labels.assert_not_called()
        jira.set_team.assert_not_called()

    def test_backfill_skips_ticket_with_opt_out_label(self, tmp_path):
        orch, _, jira, _, _ = _make_orchestrator(
            tmp_path,
            config=_team_config("auto", automation_opt_out_labels=["no-automation"]),
            team_classifier=self._classifier(["Team Alpha"]),
        )
        ticket = make_ticket("PROJ-1", "Weekly status report")
        ticket.labels = ["no-automation"]
        ticket.description = "transport reconnect backoff work"

        orch._backfill_team_labels(MEMBER, [ticket], SyncSummary())

        jira.update_labels.assert_not_called()
        jira.set_team.assert_not_called()
        assert ticket.labels == ["no-automation"]

    def test_backfill_shadow_no_write(self, tmp_path):
        pr = make_pr(number=42)
        orch, _, jira = self._orch(
            tmp_path, "shadow", self._classifier(["Team Alpha"]), pr=pr
        )
        ticket = make_ticket("PROJ-1", "Existing")
        ticket.remote_links = ["https://github.com/exampleorg/widgets/pull/42"]
        orch._backfill_team_labels(MEMBER, [ticket], SyncSummary())
        jira.update_labels.assert_not_called()
        jira.set_team.assert_not_called()

    def test_backfill_assigns_prless_ticket_from_text(self, tmp_path):
        classifier = self._classifier(["Team Alpha"])
        orch, _, jira = self._orch(tmp_path, "auto", classifier)
        ticket = make_ticket("PROJ-9", "Mitigate reconnect storms")
        ticket.description = "transport reconnect backoff work"

        orch._backfill_team_labels(MEMBER, [ticket], SyncSummary())

        classifier.classify_ordered.assert_called_once_with(
            "Mitigate reconnect storms", "transport reconnect backoff work", ()
        )
        jira.set_team.assert_called_once_with(
            "PROJ-9", "team-uuid-alpha", "customfield_11111"
        )

    def test_backfill_tags_open_containers(self, tmp_path):
        orch, _, jira = self._orch(tmp_path, "auto", self._classifier(["Team Alpha"]))
        container = make_ticket("PROJ-18", "RFC: dynamic shapes")
        container.issuetype = "Epic"
        container.description = "spanning RFC work"
        jira.get_open_containers.return_value = [container]

        orch._backfill_container_team_labels(MEMBER, SyncSummary())

        jira.get_open_containers.assert_called_once_with("octocat@example.com", "PROJ")
        jira.set_team.assert_called_once_with(
            "PROJ-18", "team-uuid-alpha", "customfield_11111"
        )

    def test_backfill_resilient_to_team_set_400(self, tmp_path):
        orch, _, jira = self._orch(tmp_path, "auto", self._classifier(["Team Alpha"]))
        jira.set_team.side_effect = _http_error(400)
        ticket = make_ticket("PROJ-1", "Existing")
        ticket.description = "core work"

        summary = SyncSummary()
        orch._backfill_team_labels(MEMBER, [ticket], summary)

        assert ticket.team_id == ""
        assert summary.errors == 0


class TestSprintMath:
    def test_current_sprint_number(self):
        anchor = date(2026, 6, 2)
        assert current_sprint_number(anchor, date(2026, 6, 16), 34) == 35
        assert current_sprint_number(anchor, anchor, 34) == 34
        assert current_sprint_number(anchor, date(2026, 6, 1), 34) is None

    def test_configurable_sprint_length(self):
        anchor = date(2026, 6, 2)
        assert current_sprint_number(anchor, date(2026, 6, 16), 34, sprint_days=7) == 36
        assert sprint_window(anchor, 34, 35, sprint_days=7) == (
            date(2026, 6, 9),
            date(2026, 6, 16),
        )

    def test_sweep_cutoff_date(self):
        anchor = date(2026, 6, 2)
        today = date(2026, 6, 16)
        assert sweep_cutoff_date(anchor, today, 1) == date(2026, 6, 2)
        assert sweep_cutoff_date(anchor, today, 0) == date(2026, 6, 16)
        assert sweep_cutoff_date(anchor, date(2026, 6, 27), 1) == date(2026, 6, 2)
        assert sweep_cutoff_date(anchor, date(2026, 6, 1), 1) is None

    def test_sprints_to_provision_and_window(self):
        anchor = date(2026, 6, 2)
        assert sprints_to_provision(anchor, date(2026, 6, 16), 34, 2) == [36, 37]
        assert sprints_to_provision(anchor, date(2026, 6, 1), 34, 2) == []
        assert sprints_to_provision(anchor, date(2026, 6, 16), 34, 0) == []
        assert sprint_window(anchor, 34, 36) == (date(2026, 6, 30), date(2026, 7, 14))


def _sprint_config(**overrides):
    return make_config(
        enable_sprint_tagging=True,
        sprint_anchor_date="2026-06-02",
        sprint_anchor_number=34,
        sprint_board_id=42,
        sprint_name_format="Sprint {number}",
        sprint_field="customfield_99001",
        **overrides,
    )


def _at_sprint_35():
    ctx = patch("upstream_jira_sync.tagging.date")
    mock_date = ctx.start()
    mock_date.today.return_value = date(2026, 6, 16)  # Sprint 35
    mock_date.fromisoformat = date.fromisoformat
    return ctx


class TestSprintTagging:
    def _tagger(self, jira, **overrides):
        return TicketTagger(
            config=_sprint_config(**overrides), github=MagicMock(), jira=jira
        )

    def test_resolve_matches_named_sprint_and_memoizes(self):
        jira = MagicMock()
        jira.get_sprint_by_name.return_value = SprintRef(id=68993, name="Sprint 35")
        tagger = self._tagger(jira)
        ctx = _at_sprint_35()
        try:
            sprint = tagger.resolve_current_sprint()
            tagger.resolve_current_sprint()
        finally:
            ctx.stop()
        jira.get_sprint_by_name.assert_called_once_with(42, "Sprint 35")
        assert sprint.id == 68993

    def test_resolve_before_anchor_returns_none(self):
        jira = MagicMock()
        tagger = self._tagger(jira)
        with patch("upstream_jira_sync.tagging.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 1)
            mock_date.fromisoformat = date.fromisoformat
            assert tagger.resolve_current_sprint() is None
        jira.get_sprint_by_name.assert_not_called()

    def test_provision_creates_only_missing_sprints(self):
        def lookup(_board, name):
            return SprintRef(id=1, name=name) if name.endswith("36") else None

        jira = MagicMock()
        jira.get_sprint_by_name.side_effect = lookup
        tagger = self._tagger(
            jira,
            enable_sprint_provision=True,
            sprint_provision_mode="auto",
            sprint_provision_lookahead=2,
        )
        summary = SyncSummary()
        ctx = _at_sprint_35()
        try:
            tagger.provision_future_sprints(summary)
        finally:
            ctx.stop()
        jira.create_sprint.assert_called_once_with(
            42, "Sprint 37", date(2026, 7, 14), date(2026, 7, 28)
        )
        assert summary.sprint_provisioned == 1


class TestSprintSweep:
    def _sweep(self, jira, candidates, sweep_mode="auto"):
        config = _sprint_config(enable_sprint_sweep=True, sprint_sweep_mode=sweep_mode)
        jira.get_sprint_sweep_candidates.return_value = candidates
        tagger = TicketTagger(config=config, github=MagicMock(), jira=jira)
        summary = SyncSummary()
        ctx = _at_sprint_35()
        try:
            tagger.sweep_sprint(MEMBER, summary)
        finally:
            ctx.stop()
        return summary

    @staticmethod
    def _jira_with_sprint():
        jira = MagicMock()
        jira.get_sprint_by_name.return_value = SprintRef(id=68993, name="Sprint 35")
        return jira

    def test_sweep_adds_ticket_not_in_sprint(self):
        jira = self._jira_with_sprint()
        ticket = make_ticket("PROJ-1", "x")
        summary = self._sweep(jira, [ticket])
        jira.add_issues_to_sprint.assert_called_once_with(68993, ["PROJ-1"])
        assert 68993 in ticket.sprint_ids
        assert summary.sprint_swept == 1

    def test_sweep_idempotent_skip(self):
        jira = self._jira_with_sprint()
        ticket = make_ticket("PROJ-1", "x")
        ticket.sprint_ids = {68993}
        summary = self._sweep(jira, [ticket])
        jira.add_issues_to_sprint.assert_not_called()
        assert summary.sprint_swept == 0

    def test_sweep_shadow_no_write(self):
        jira = self._jira_with_sprint()
        ticket = make_ticket("PROJ-1", "x")
        self._sweep(jira, [ticket], sweep_mode="shadow")
        jira.add_issues_to_sprint.assert_not_called()
        assert 68993 not in ticket.sprint_ids

    def test_sweep_403_bumps_errors_but_400_does_not(self):
        jira = self._jira_with_sprint()
        jira.add_issues_to_sprint.side_effect = _http_error(403)
        assert self._sweep(jira, [make_ticket("PROJ-1", "x")]).errors == 1

        jira = self._jira_with_sprint()
        jira.add_issues_to_sprint.side_effect = _http_error(400)
        summary = self._sweep(jira, [make_ticket("PROJ-1", "x")])
        assert summary.errors == 0
        assert summary.sprint_swept == 0

    def test_sweep_skips_ticket_with_opt_out_label(self):
        jira = self._jira_with_sprint()
        ticket = make_ticket("PROJ-1", "Weekly status report")
        ticket.labels = ["weekly-status", "no-automation"]
        config = _sprint_config(
            enable_sprint_sweep=True,
            sprint_sweep_mode="auto",
            automation_opt_out_labels=["no-automation"],
        )
        jira.get_sprint_sweep_candidates.return_value = [ticket]
        tagger = TicketTagger(config=config, github=MagicMock(), jira=jira)
        summary = SyncSummary()
        ctx = _at_sprint_35()
        try:
            tagger.sweep_sprint(MEMBER, summary)
        finally:
            ctx.stop()
        jira.add_issues_to_sprint.assert_not_called()
        assert 68993 not in ticket.sprint_ids
        assert summary.sprint_swept == 0

    def test_sweep_opt_out_match_is_case_insensitive(self):
        jira = self._jira_with_sprint()
        ticket = make_ticket("PROJ-1", "Weekly status report")
        ticket.labels = ["No-Automation"]
        config = _sprint_config(
            enable_sprint_sweep=True,
            sprint_sweep_mode="auto",
            automation_opt_out_labels=["no-automation"],
        )
        jira.get_sprint_sweep_candidates.return_value = [ticket]
        tagger = TicketTagger(config=config, github=MagicMock(), jira=jira)
        ctx = _at_sprint_35()
        try:
            tagger.sweep_sprint(MEMBER, SyncSummary())
        finally:
            ctx.stop()
        jira.add_issues_to_sprint.assert_not_called()

    def test_sweep_still_adds_unlabelled_ticket_when_opt_out_configured(self):
        jira = self._jira_with_sprint()
        ticket = make_ticket("PROJ-1", "Real work")
        config = _sprint_config(
            enable_sprint_sweep=True,
            sprint_sweep_mode="auto",
            automation_opt_out_labels=["no-automation"],
        )
        jira.get_sprint_sweep_candidates.return_value = [ticket]
        tagger = TicketTagger(config=config, github=MagicMock(), jira=jira)
        summary = SyncSummary()
        ctx = _at_sprint_35()
        try:
            tagger.sweep_sprint(MEMBER, summary)
        finally:
            ctx.stop()
        jira.add_issues_to_sprint.assert_called_once_with(68993, ["PROJ-1"])
        assert summary.sprint_swept == 1

    def test_sweep_queries_with_cutoff_and_status_map_names(self):
        jira = self._jira_with_sprint()
        self._sweep(jira, [])
        args = jira.get_sprint_sweep_candidates.call_args
        assert args.args[0] == "octocat@example.com"
        assert args.args[1] == "PROJ"
        assert args.args[2] == ["In Progress", "In Review"]
        assert args.args[3] == "2026-06-02"


class TestAddToCurrentSprint:
    def _orch(self, jira, sprint_mode="auto"):
        orch = SyncOrchestrator(
            config=_sprint_config(sprint_mode=sprint_mode),
            github=MagicMock(),
            jira=jira,
            matcher=MagicMock(),
            resolver=MagicMock(),
            state=MagicMock(),
        )
        jira.get_sprint_by_name.return_value = SprintRef(id=68993, name="Sprint 35")
        return orch

    def _add(self, orch, ticket):
        summary = SyncSummary()
        ctx = _at_sprint_35()
        try:
            orch._add_to_current_sprint(ticket, summary)
        finally:
            ctx.stop()
        return summary

    def test_add_auto_calls_jira(self):
        jira = MagicMock()
        orch = self._orch(jira)
        summary = self._add(orch, make_ticket("PROJ-9", "x"))
        jira.add_issues_to_sprint.assert_called_once_with(68993, ["PROJ-9"])
        assert summary.errors == 0

    def test_add_shadow_no_write(self):
        jira = MagicMock()
        orch = self._orch(jira, sprint_mode="shadow")
        self._add(orch, make_ticket("PROJ-9", "x"))
        jira.add_issues_to_sprint.assert_not_called()

    def test_add_403_bumps_errors_but_400_does_not(self):
        jira = MagicMock()
        jira.add_issues_to_sprint.side_effect = _http_error(400)
        summary = self._add(self._orch(jira), make_ticket("PROJ-9", "x"))
        assert summary.errors == 0

        jira = MagicMock()
        jira.add_issues_to_sprint.side_effect = _http_error(403)
        summary = self._add(self._orch(jira), make_ticket("PROJ-9", "x"))
        assert summary.errors == 1


class TestManualOverrideGate:
    BOT_EMAIL = "bot@example.com"
    BOT_ACCOUNT = "acct-bot"
    SP_FIELD = "customfield_12345"

    def _make_jira_mock(self, search_response: dict):
        jira = JiraClient.__new__(JiraClient)
        jira._base = "https://jira.test"
        resp = MagicMock()
        resp.json.return_value = search_response
        jira._request = MagicMock(return_value=resp)
        return jira

    def _history(self, created, author, items):
        return {"created": created, "author": author, "items": items}

    def test_prefetch_caches_humans(self):
        jira = self._make_jira_mock(
            {
                "issues": [
                    {
                        "key": "T-1",
                        "changelog": {
                            "total": 2,
                            "maxResults": 10,
                            "histories": [
                                self._history(
                                    "2026-01-01T00:00:00Z",
                                    {"emailAddress": "human@example.com"},
                                    [
                                        {
                                            "fieldId": self.SP_FIELD,
                                            "field": "Story Points",
                                        }
                                    ],
                                ),
                                self._history(
                                    "2026-01-02T00:00:00Z",
                                    {"emailAddress": "alice@example.com"},
                                    [{"field": "status"}],
                                ),
                            ],
                        },
                    },
                    {
                        "key": "T-2",
                        "changelog": {
                            "total": 1,
                            "maxResults": 10,
                            "histories": [
                                self._history(
                                    "2026-01-03T00:00:00Z",
                                    {"emailAddress": self.BOT_EMAIL},
                                    [{"fieldId": self.SP_FIELD}],
                                )
                            ],
                        },
                    },
                ]
            }
        )
        gate = ManualOverrideGate(jira, self.BOT_EMAIL, self.BOT_ACCOUNT)
        gate.prefetch(["T-1", "T-2"], {self.SP_FIELD, "status"})

        assert gate.is_human_owned("T-1", self.SP_FIELD) is True
        assert gate.is_human_owned("T-1", "status") is True
        assert gate.is_human_owned("T-2", self.SP_FIELD) is False

    def test_truncated_changelog_fails_open(self, caplog):
        jira = self._make_jira_mock(
            {
                "issues": [
                    {
                        "key": "T-9",
                        "changelog": {
                            "total": 200,
                            "maxResults": 100,
                            "histories": [
                                self._history(
                                    "2026-01-01T00:00:00Z",
                                    {"emailAddress": "human@example.com"},
                                    [{"fieldId": self.SP_FIELD}],
                                )
                            ],
                        },
                    }
                ]
            }
        )
        gate = ManualOverrideGate(jira, self.BOT_EMAIL, self.BOT_ACCOUNT)
        gate.prefetch(["T-9"], {self.SP_FIELD})

        with caplog.at_level("WARNING"):
            assert gate.is_human_owned("T-9", self.SP_FIELD) is False
        assert gate.is_unreliable("T-9") is True

    def test_missing_prefetch_fails_open(self, caplog):
        jira = self._make_jira_mock({"issues": []})
        gate = ManualOverrideGate(jira, self.BOT_EMAIL, self.BOT_ACCOUNT)
        with caplog.at_level("WARNING"):
            assert gate.is_human_owned("T-404", self.SP_FIELD) is False
        assert any("without prefetch" in r.message.lower() for r in caplog.records)

    def test_bot_edit_clears_protection(self):
        jira = self._make_jira_mock(
            {
                "issues": [
                    {
                        "key": "T-5",
                        "changelog": {
                            "total": 2,
                            "maxResults": 10,
                            "histories": [
                                self._history(
                                    "2026-01-01T00:00:00Z",
                                    {"emailAddress": "human@example.com"},
                                    [{"fieldId": self.SP_FIELD}],
                                ),
                                self._history(
                                    "2026-01-02T00:00:00Z",
                                    {
                                        "emailAddress": self.BOT_EMAIL,
                                        "accountId": self.BOT_ACCOUNT,
                                    },
                                    [{"fieldId": self.SP_FIELD}],
                                ),
                            ],
                        },
                    }
                ]
            }
        )
        gate = ManualOverrideGate(jira, self.BOT_EMAIL, self.BOT_ACCOUNT)
        gate.prefetch(["T-5"], {self.SP_FIELD})
        assert gate.is_human_owned("T-5", self.SP_FIELD) is False

    def test_empty_author_not_human(self):
        jira = self._make_jira_mock(
            {
                "issues": [
                    {
                        "key": "T-7",
                        "changelog": {
                            "total": 2,
                            "maxResults": 10,
                            "histories": [
                                self._history(
                                    "2026-01-01T00:00:00Z",
                                    None,
                                    [{"fieldId": self.SP_FIELD}],
                                ),
                                self._history(
                                    "2026-01-02T00:00:00Z",
                                    {},
                                    [{"fieldId": self.SP_FIELD}],
                                ),
                            ],
                        },
                    }
                ]
            }
        )
        gate = ManualOverrideGate(jira, self.BOT_EMAIL, self.BOT_ACCOUNT)
        gate.prefetch(["T-7"], {self.SP_FIELD})
        assert gate.is_human_owned("T-7", self.SP_FIELD) is False


class TestManualOverrideOrchestrator:
    SP_FIELD = "customfield_12345"

    def _make_gate(self, *, is_human: bool):
        gate = MagicMock()
        gate.is_human_owned.return_value = is_human
        return gate

    def _orch(
        self,
        tmp_path,
        *,
        gate=None,
        mode="auto",
        fields=("status",),
        target_status=CanonicalStatus.REVIEW,
        estimator=None,
    ):
        config = make_config(
            manual_override_mode=mode,
            manual_override_fields=list(fields),
            enable_estimation=estimator is not None,
            story_points_field=self.SP_FIELD,
        )
        pr = make_pr(number=42)
        ticket = make_ticket("PROJ-100", "Test ticket")
        orch, _, jira, _, state = _make_orchestrator(
            tmp_path,
            pr_reviews=[PRWithReview(pr, ReviewDecision.NONE, 0)],
            tickets=[ticket],
            match_result=MatchResult(ticket=ticket, confidence="high", reason="t"),
            target_status=target_status,
            estimator=estimator,
            override_gate=gate,
            config=config,
        )
        return orch, jira, state, pr, ticket

    def test_first_encounter_writes_baseline(self, tmp_path):
        gate = self._make_gate(is_human=True)
        orch, jira, state, pr, ticket = self._orch(tmp_path, gate=gate)

        assert state.get_pr_state_snapshot(ticket.key) is None
        orch.run()

        jira.transition_ticket.assert_called_once()
        snapshot = state.get_pr_state_snapshot(ticket.key)
        assert snapshot is not None
        assert snapshot["resolved_status"] == CanonicalStatus.REVIEW.value
        assert snapshot["pr_url"] == pr.url

    def test_status_skipped_when_intent_unchanged(self, tmp_path):
        gate = self._make_gate(is_human=True)
        orch, jira, state, pr, ticket = self._orch(tmp_path, gate=gate)
        state.record_pr_state_snapshot(
            ticket_key=ticket.key,
            pr_state="OPEN",
            pr_url=pr.url,
            resolved_status=CanonicalStatus.REVIEW.value,
        )

        orch.run()
        jira.transition_ticket.assert_not_called()

    def test_status_written_when_intent_changes(self, tmp_path):
        gate = self._make_gate(is_human=True)
        orch, jira, state, pr, ticket = self._orch(
            tmp_path, gate=gate, target_status=CanonicalStatus.IN_PROGRESS
        )
        state.record_pr_state_snapshot(
            ticket_key=ticket.key,
            pr_state="OPEN",
            pr_url=pr.url,
            resolved_status=CanonicalStatus.REVIEW.value,
        )

        orch.run()

        jira.transition_ticket.assert_called_once()
        assert (
            state.get_pr_state_snapshot(ticket.key)["resolved_status"]
            == CanonicalStatus.IN_PROGRESS.value
        )

    def test_story_points_skipped_in_auto_mode(self, tmp_path):
        gate = self._make_gate(is_human=True)
        estimator = MagicMock()
        estimator.estimate.return_value = 5
        orch, jira, _, _, _ = self._orch(
            tmp_path, gate=gate, fields=("status", self.SP_FIELD), estimator=estimator
        )

        orch.run()

        estimator.estimate.assert_called_once()
        jira.set_story_points.assert_not_called()

    def test_disabled_gate_preserves_behavior(self, tmp_path):
        orch, jira, state, _, ticket = self._orch(tmp_path, gate=None)
        orch.run()
        jira.transition_ticket.assert_called_once()
        assert state.get_pr_state_snapshot(ticket.key) is None

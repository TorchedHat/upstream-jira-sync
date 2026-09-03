from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Final

from upstream_jira_sync.config import TeamSpec
from upstream_jira_sync.llm import LLMFatalError, LLMProvider
from upstream_jira_sync.models import (
    MAX_COMMENT_CHARS,
    MAX_ISSUE_BODY_CHARS,
    MAX_PR_BODY_CHARS,
    MAX_TICKET_DESC_CHARS,
    VALID_STORY_POINTS,
    ClaimResult,
    GitHubIssue,
    JiraTicket,
    MatchResult,
    PullRequest,
)
from upstream_jira_sync.skill_loader import SkillLoader, strip_markdown_fences
from upstream_jira_sync.teams import canonical_team_names, render_team_prompt_section

log = logging.getLogger(__name__)


class _SkillBasedAI:
    """Base for AI classes that load their system prompt from a skill file."""

    _SKILL_NAME: str = ""

    def __init__(self, llm: LLMProvider, skill_loader: SkillLoader) -> None:
        self._llm = llm
        self._system = skill_loader.load(self._SKILL_NAME)


class AITicketMatcher(_SkillBasedAI):
    """Matches a PR to a Jira ticket: deterministic URL match first, LLM fallback."""

    _SKILL_NAME = "ticket_matcher"
    _PROMPT_TEMPLATE: Final[str] = """GitHub PR:
Title: {title}
Description: {body}
Linked GitHub issues: {pr_links}

Candidate Jira tickets assigned to this engineer:
{tickets}

Return a JSON object with exactly these fields:
{{
  "key": "<JIRA-KEY or null>",
  "confidence": "<high|medium|low>",
  "reason": "<one sentence>"
}}

Rules:
- If a candidate ticket's linked GitHub URLs overlap with the PR's linked issues, that is strong evidence -- return high confidence.
- Otherwise compare the PR's title/body against each ticket's summary AND description. The description carries the actual scope; the summary is just a label.
- Return a key ONLY at "high" confidence; for "medium" or "low", return null.
- If the ticket description is missing, vague, generic, or doesn't specifically describe the PR's work, return null.
- Never guess. No match is better than a wrong match.
"""

    def find_best(
        self,
        pr: PullRequest,
        tickets: list[JiraTicket],
    ) -> MatchResult | None:
        """Try URL-based deterministic match first, then fall back to the LLM."""
        if not tickets:
            return None

        url_match = self._find_by_linked_urls(pr, tickets)
        if url_match:
            return url_match

        ticket_list = "\n".join(
            f"{i + 1}. {t.key}: {t.summary}"
            + (
                f"\n   Description: {t.description[:MAX_TICKET_DESC_CHARS]}"
                if t.description
                else ""
            )
            + (f"\n   Linked: {', '.join(t.remote_links)}" if t.remote_links else "")
            for i, t in enumerate(tickets)
        )
        pr_links = (
            ", ".join(li.url for li in pr.linked_issues)
            if pr.linked_issues
            else "(none)"
        )
        prompt = self._PROMPT_TEMPLATE.format(
            title=pr.title,
            body=pr.body.strip() or "(no description provided)",
            pr_links=pr_links,
            tickets=ticket_list,
        )

        try:
            raw = self._llm.complete(
                system=self._system,
                user_message=prompt,
            )
            raw = strip_markdown_fences(raw)
            result = json.loads(raw)
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning(
                "  AI match failed for PR #%d (%s) -- skipping.",
                pr.number,
                exc,
            )
            return None

        key = result.get("key")
        confidence = result.get("confidence", "low")
        reason = result.get("reason", "")

        if not key or confidence != "high":
            log.warning(
                "  No confident AI match for PR #%d '%s' (confidence=%s from %d candidates) -- %s",
                pr.number,
                pr.title,
                confidence,
                len(tickets),
                reason or "(no reason given)",
            )
            return None

        matched = next((t for t in tickets if t.key == key), None)
        if not matched:
            log.warning(
                "  Model returned key '%s' which is not in the "
                "candidate list for PR #%d. Skipping.",
                key,
                pr.number,
            )
            return None

        log.info(
            "  AI matched PR #%d '%s' -> %s [%s] -- %s",
            pr.number,
            pr.title,
            matched.key,
            confidence,
            reason,
        )
        return MatchResult(ticket=matched, confidence=confidence, reason=reason)

    @staticmethod
    def _find_by_linked_urls(
        pr: PullRequest,
        tickets: list[JiraTicket],
    ) -> MatchResult | None:
        """Deterministic match: PR's linked issue URL appears in a ticket's remote links."""
        if not pr.linked_issues:
            return None
        pr_urls = {li.url for li in pr.linked_issues}
        for t in tickets:
            overlap = pr_urls.intersection(t.remote_links)
            if overlap:
                url = next(iter(overlap))
                log.info(
                    "  URL matched PR #%d -> %s via %s",
                    pr.number,
                    t.key,
                    url,
                )
                return MatchResult(
                    ticket=t,
                    confidence="high",
                    reason=f"PR linked issue {url} is already linked on {t.key}.",
                )
        return None


class StoryPointEstimator(_SkillBasedAI):
    _SKILL_NAME = "story_point_estimation"
    _PR_PROMPT: Final[str] = """Jira ticket: {ticket_key}: {ticket_summary}

GitHub PR:
Title: {pr_title}
Description: {pr_body}

Estimate story points using the scoring checklist."""

    _ISSUE_PROMPT: Final[
        str
    ] = """Jira ticket (provisional -- work not yet started): {ticket_key}: {ticket_summary}

GitHub issue being claimed:
Title: {issue_title}
Description: {issue_body}

Estimate provisional story points from the issue description alone. No implementation exists yet -- treat this as a planning estimate based on described scope and complexity."""

    def estimate(self, pr: PullRequest, ticket: JiraTicket) -> int | None:
        prompt = self._PR_PROMPT.format(
            ticket_key=ticket.key,
            ticket_summary=ticket.summary,
            pr_title=pr.title,
            pr_body=pr.body.strip() or "(no description provided)",
        )
        return self._run(prompt, ticket.key, context=f"PR #{pr.number}")

    def estimate_from_issue(
        self,
        ticket: JiraTicket,
        issue_title: str,
        issue_body: str,
    ) -> int | None:
        """Provisional estimate at claim-time, before any PR exists."""
        prompt = self._ISSUE_PROMPT.format(
            ticket_key=ticket.key,
            ticket_summary=ticket.summary,
            issue_title=issue_title,
            issue_body=(issue_body.strip() or "(no description provided)")[
                :MAX_ISSUE_BODY_CHARS
            ],
        )
        return self._run(prompt, ticket.key, context="issue claim")

    def _run(self, prompt: str, ticket_key: str, context: str) -> int | None:
        try:
            raw = self._llm.complete(
                system=self._system,
                user_message=prompt,
                max_tokens=256,
            )
            raw = strip_markdown_fences(raw)
            result = json.loads(raw)
            points = int(result.get("points", 0))
            reason = result.get("reason", "")
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning(
                "  Story point estimation failed for %s (%s): %s",
                ticket_key,
                context,
                exc,
            )
            return None

        if points not in VALID_STORY_POINTS:
            log.warning(
                "  Model returned invalid points %d for %s, skipping.",
                points,
                ticket_key,
            )
            return None

        log.info(
            "  Estimated %s at %d point(s) — %s",
            ticket_key,
            points,
            reason,
        )
        return points


class IssueSummarizer(_SkillBasedAI):
    _SKILL_NAME = "issue_summarizer"
    _PROMPT_TEMPLATE: Final[str] = """Title: {title}

Body:
{body}"""

    def summarize(self, title: str, body: str) -> str:
        try:
            prompt = self._PROMPT_TEMPLATE.format(
                title=title,
                body=(body.strip() or "(no description provided)")[
                    :MAX_ISSUE_BODY_CHARS
                ],
            )
            return self._llm.complete(
                system=self._system,
                user_message=prompt,
                max_tokens=256,
            )
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning("  Summarization failed (%s)", exc)
            return ""


class WeeklyDigestSummarizer(_SkillBasedAI):
    """Turns a structured delta table into a neutral 3-5 sentence narrative."""

    _SKILL_NAME = "weekly_digest_summary"

    def summarize(self, events_json: str) -> str:
        try:
            return self._llm.complete(
                system=self._system,
                user_message=events_json,
                max_tokens=600,
            )
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning("  Digest summarization failed (%s)", exc)
            return ""


class RfcClassifier(_SkillBasedAI):
    """Decides whether an RFC-titled issue is tracked as a container issue or a story."""

    _SKILL_NAME = "rfc_classifier"
    _PROMPT_TEMPLATE: Final[str] = """Title: {title}
Body: {body}

Classify this issue."""

    def classify(self, title: str, body: str) -> str | None:
        """Returns 'epic' or 'story', or None on classification error (caller should retry next run)."""
        prompt = self._PROMPT_TEMPLATE.format(
            title=title,
            body=(body or "")[:MAX_ISSUE_BODY_CHARS],
        )
        try:
            raw = self._llm.complete(
                system=self._system,
                user_message=prompt,
                max_tokens=128,
            )
            result = json.loads(strip_markdown_fences(raw))
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning("  RFC classification failed for %r (%s)", title[:80], exc)
            return None
        verdict = result.get("verdict", "")
        if verdict not in ("epic", "story"):
            log.warning("  RFC classifier returned unknown verdict %r", verdict)
            return None
        log.info(
            "  RFC check %r: %s -- %s", title[:60], verdict, result.get("reason", "")
        )
        return verdict


class IssueClaimClassifier(_SkillBasedAI):
    _SKILL_NAME = "issue_claim_classifier"
    _PROMPT_TEMPLATE: Final[str] = """GitHub Issue: #{number} — {title}

Comment by {username}:
{comment}

Classify this comment."""

    def classify(
        self,
        issue: GitHubIssue,
        comment: str,
        username: str,
    ) -> ClaimResult:
        try:
            return self._ai_classify(issue, comment, username)
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning(
                "  Claim classification failed for issue #%d (%s)",
                issue.number,
                exc,
            )
            return ClaimResult(intent="not_claiming", reason="Classification error")

    def _ai_classify(
        self,
        issue: GitHubIssue,
        comment: str,
        username: str,
    ) -> ClaimResult:
        prompt = self._PROMPT_TEMPLATE.format(
            number=issue.number,
            title=issue.title,
            username=username,
            comment=comment[:MAX_COMMENT_CHARS],
        )

        raw = self._llm.complete(
            system=self._system,
            user_message=prompt,
            max_tokens=128,
        )

        raw = strip_markdown_fences(raw)
        result = json.loads(raw)
        intent = result.get("intent", "not_claiming")
        reason = result.get("reason", "")

        if intent not in ("claiming", "not_claiming"):
            intent = "not_claiming"

        log.info(
            "  Issue #%d comment by @%s: %s — %s",
            issue.number,
            username,
            intent,
            reason,
        )
        return ClaimResult(intent=intent, reason=reason)


class IssueDeduplicator(_SkillBasedAI):
    """Checks whether a GitHub issue is already tracked by an existing Jira ticket."""

    _SKILL_NAME = "issue_dedup_matcher"
    _PROMPT_TEMPLATE: Final[str] = """GitHub Issue:
Title: {title}
Description: {body}

Candidate Jira tickets (recent, may be stale or unlinked):
{tickets}

Return a JSON object with exactly these fields:
{{
  "key": "<JIRA-KEY or null>",
  "confidence": "<high|medium|low>",
  "reason": "<one sentence>"
}}

Rules:
- Only return a key from the candidate list. Never invent ticket keys.
- Return a key ONLY at "high" confidence; for "medium" or "low", return null.
- If two or more candidates are comparably plausible, return null.
- An epic or broad umbrella ticket is not a match -- return null.
"""

    def find_existing(
        self,
        issue_title: str,
        issue_body: str,
        tickets: list[JiraTicket],
    ) -> MatchResult | None:
        """Return the ticket already tracking this issue (high confidence only)."""
        if not tickets:
            return None

        ticket_list = "\n".join(
            f"{i + 1}. {t.key} [{t.status}]: {t.summary}" for i, t in enumerate(tickets)
        )
        prompt = self._PROMPT_TEMPLATE.format(
            title=issue_title,
            body=(issue_body.strip() or "(no description provided)")[
                :MAX_ISSUE_BODY_CHARS
            ],
            tickets=ticket_list,
        )

        try:
            raw = self._llm.complete(
                system=self._system,
                user_message=prompt,
                max_tokens=192,
            )
            raw = strip_markdown_fences(raw)
            result = json.loads(raw)
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning(
                "  Issue dedup check failed (%s) -- assuming not a duplicate.", exc
            )
            return None

        key = result.get("key")
        confidence = result.get("confidence", "low")
        reason = result.get("reason", "")

        if not key or confidence != "high":
            return None

        matched = next((t for t in tickets if t.key == key), None)
        if not matched:
            log.warning(
                "  Dedup returned key '%s' which is not in the candidate list. Skipping.",
                key,
            )
            return None

        log.info(
            "  Issue already tracked by %s [%s] -- %s", matched.key, confidence, reason
        )
        return MatchResult(ticket=matched, confidence=confidence, reason=reason)


class TeamClassifier:
    """Classifies a PR into one or more configured teams via the LLM (R7)."""

    _SKILL_NAME = "team_classification"
    _JSON_ARRAY_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]", re.DOTALL)

    def __init__(
        self,
        llm: LLMProvider,
        skill_loader: SkillLoader,
        teams: Sequence[TeamSpec],
    ) -> None:
        self._llm = llm
        self._prompt = skill_loader.load(self._SKILL_NAME)
        self._teams_section = render_team_prompt_section(teams)
        self._canonical_teams = canonical_team_names(teams)

    def classify_ordered(
        self,
        pr_title: str,
        pr_body: str,
        file_paths: tuple[str, ...],
    ) -> list[str]:
        """Canonical team names for this PR, primary owner first. Empty on failure or uncertainty."""
        body = pr_body or ""
        if len(body) > MAX_PR_BODY_CHARS:
            body = body[:MAX_PR_BODY_CHARS] + "\n[...body truncated...]"
        if file_paths:
            shown = list(file_paths[:50])
            if len(file_paths) > 50:
                shown.append(f"... ({len(file_paths) - 50} more files truncated)")
            paths = "\n".join(shown)
        else:
            paths = "(no files)"
        prompt = self._prompt.format(
            teams_section=self._teams_section,
            pr_title=pr_title,
            pr_body=body,
            file_paths=paths,
        )
        try:
            response = self._llm.complete(
                system="You are a multi-label classifier. Output only valid JSON.",
                user_message=prompt,
                max_tokens=256,
            )
        except LLMFatalError:
            raise
        except Exception as exc:
            log.warning("  TeamClassifier API call failed: %s", exc)
            return []

        match = self._JSON_ARRAY_RE.search(response)
        if not match:
            return []
        try:
            raw = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(raw, list):
            return []

        result: list[str] = []
        for name in raw:
            if isinstance(name, str) and name in self._canonical_teams:
                if name not in result:
                    result.append(name)
            elif isinstance(name, str):
                log.warning("  TeamClassifier returned unknown team %r", name)
        return result

    def classify(
        self,
        pr_title: str,
        pr_body: str,
        file_paths: tuple[str, ...],
    ) -> set[str]:
        """Set of canonical team names for this PR. Back-compat view of classify_ordered."""
        return set(self.classify_ordered(pr_title, pr_body, file_paths))

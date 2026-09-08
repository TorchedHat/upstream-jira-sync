from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from time import sleep
from typing import Any, Callable, Final

import requests

from upstream_jira_sync.http import BaseHTTPClient
from upstream_jira_sync.models import (
    MAX_GRAPHQL_PAGES,
    MAX_PR_BODY_CHARS,
    GitHubIssue,
    LinkedIssue,
    PRWithReview,
    PullRequest,
    ReviewDecision,
    repo_from_github_url,
    sanitize_identifier,
)

log = logging.getLogger(__name__)

_CLOSING_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]*(?:issue\s+)?#(\d+)",
    re.IGNORECASE,
)
_HTML_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCED_CODE_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)
_BLOCKQUOTE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*>.*$", re.MULTILINE)


def _normalize_utc_timestamp(timestamp: str | None) -> str:
    """Normalize a Z-suffixed GitHub timestamp to +00:00 offset ISO form."""
    if not timestamp:
        return ""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).isoformat()


def _state_from_gql(gql_state: str) -> tuple[str, bool]:
    """GraphQL PR state -> (our `state`, `merged`)."""
    if gql_state == "MERGED":
        return "closed", True
    if gql_state == "CLOSED":
        return "closed", False
    return "open", False


def _labels_from_node(node: dict) -> tuple[str, ...]:
    """Extract label names from a GraphQL PR node, tolerating missing/null fields."""
    nodes = (node.get("labels") or {}).get("nodes") or []
    return tuple(n["name"] for n in nodes if n and n.get("name"))


def _strip_non_prose(body: str) -> str:
    """Remove HTML comments, fenced code blocks, and blockquotes before regex scan."""
    body = _HTML_COMMENT_RE.sub("", body)
    body = _FENCED_CODE_RE.sub("", body)
    body = _BLOCKQUOTE_RE.sub("", body)
    return body


def _is_bot_author(author: dict | None, ignored_logins: frozenset[str]) -> bool:
    """App-typed bots, [bot]-suffixed logins, and configured bot accounts."""
    author = author or {}
    login = (author.get("login") or "").lower()
    return (
        author.get("__typename") == "Bot"
        or login.endswith("[bot]")
        or login in ignored_logins
    )


def _node_list(node: dict, key: str) -> list[dict]:
    """Non-null entries of a GraphQL `{key: {nodes: [...]}}` connection."""
    return [n for n in ((node.get(key) or {}).get("nodes") or []) if n]


def _last_human_activity(node: dict, ignored_logins: frozenset[str]) -> str:
    """Latest PR timestamp not attributable to a bot: creation, close, head commit,
    non-bot comments and reviews. Empty string when the node has no such fields
    (GitHub timestamps share one format, so plain string max is safe)."""

    def is_human(n: dict) -> bool:
        return not _is_bot_author(n.get("author"), ignored_logins)

    return max(
        node.get("createdAt") or "",
        node.get("closedAt") or "",
        *(
            (c.get("commit") or {}).get("committedDate") or ""
            for c in _node_list(node, "commits")
        ),
        *(
            c.get("createdAt") or ""
            for c in _node_list(node, "comments")
            if is_human(c)
        ),
        *(
            r.get("submittedAt") or ""
            for r in _node_list(node, "reviews")
            if is_human(r)
        ),
    )


def _commit_author_logins(
    node: dict, ignored_logins: frozenset[str]
) -> tuple[str, ...]:
    """Unique commit-author logins in first-seen order; null users, [bot]-suffixed
    logins, and configured bot accounts excluded."""
    logins: list[str] = []
    seen: set[str] = set()
    for c in _node_list(node, "commits"):
        authors = ((c.get("commit") or {}).get("authors") or {}).get("nodes") or []
        for author in authors:
            login = ((author or {}).get("user") or {}).get("login") or ""
            key = login.lower()
            if not login or key in seen:
                continue
            if _is_bot_author({"login": login}, ignored_logins):
                continue
            seen.add(key)
            logins.append(login)
    return tuple(logins)


def _parse_closing_refs(body: str) -> list[int]:
    """Extract issue numbers referenced via closing keywords in PR body prose."""
    if not body:
        return []
    cleaned = _strip_non_prose(body)
    numbers: list[int] = []
    seen: set[int] = set()
    for match in _CLOSING_KEYWORD_RE.finditer(cleaned):
        n = int(match.group(1))
        if n not in seen:
            seen.add(n)
            numbers.append(n)
    return numbers


# Fields both PR queries fetch to compute last_human_activity_at; must stay in
# sync with _last_human_activity().
_PR_ACTIVITY_FIELDS: Final[str] = """
            createdAt
            closedAt
            comments(last: 30) {
              nodes {
                createdAt
                author { login __typename }
              }
            }
            commits(last: 30) {
              nodes {
                commit {
                  committedDate
                  authors(first: 5) { nodes { user { login } } }
                }
              }
            }
            reviews(last: 50) {
              nodes {
                author { login __typename }
                state
                submittedAt
              }
            }
"""


class GitHubClient(BaseHTTPClient):
    """Wraps the GitHub GraphQL API for reading PR and review data."""

    # Class-level default so partially-constructed clients (tests) parse safely.
    _ignore_activity_authors: frozenset[str] = frozenset()

    _SEARCH_QUERY: Final[str] = (
        """
    query($query: String!, $after: String) {
      search(query: $query, type: ISSUE, first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          ... on PullRequest {
            number
            title
            url
            state
            isDraft
            merged
            updatedAt
            body
            author { login }
            labels(first: 20) { nodes { name } }
            reviewDecision
"""
        + _PR_ACTIVITY_FIELDS
        + """
            closingIssuesReferences(first: 10) {
              nodes {
                number
                title
                url
                body
              }
            }
          }
        }
      }
    }
    """
    )

    def __init__(
        self,
        token: str,
        repos: list[str],
        merge_labels: tuple[str, ...] = (),
        base_url: str = "",
        ignore_activity_authors: list[str] | None = None,
    ) -> None:
        super().__init__()
        if not repos:
            raise ValueError("GitHubClient requires at least one repo")
        self._repos = list(repos)
        self._ignore_activity_authors = frozenset(
            a.lower() for a in (ignore_activity_authors or [])
        )
        self._merge_labels = tuple(merge_labels)
        self._api_base = (base_url or "https://api.github.com").rstrip("/")
        self._graphql_url = f"{self._api_base}/graphql"
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
            }
        )

    @property
    def _repo_qualifier(self) -> str:
        """GitHub search qualifier joining all configured repos as OR."""
        return " ".join(f"repo:{r}" for r in self._repos)

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict:
        """Execute a GraphQL query and return the data dict."""
        resp = self._request(
            "POST",
            self._graphql_url,
            json={"query": query, "variables": variables},
        )
        result = resp.json()

        if "errors" in result:
            raise RuntimeError(
                f"GraphQL errors: {json.dumps(result['errors'], indent=2)}"
            )

        data = result.get("data")
        if data is None:
            raise RuntimeError(
                "GraphQL returned null data — check authentication and query syntax."
            )

        return data

    def _paginated_search(
        self,
        graphql_query: str,
        search_query: str,
        process_node: Callable[[dict], Any | None],
        *,
        extra_variables: dict[str, Any] | None = None,
        log_context: str | None = None,
    ) -> list:
        """Run a paginated GraphQL search, processing each node via callback."""
        results: list = []
        after: str | None = None

        for _page in range(MAX_GRAPHQL_PAGES):
            variables: dict[str, Any] = {"query": search_query}
            if extra_variables:
                variables.update(extra_variables)
            if after:
                variables["after"] = after

            data = self._graphql(graphql_query, variables)
            search = data["search"]

            for node in search["nodes"]:
                if not node or "number" not in node:
                    continue
                item = process_node(node)
                if item is not None:
                    results.append(item)

            if not search["pageInfo"]["hasNextPage"]:
                break
            after = search["pageInfo"]["endCursor"]
        else:
            log.warning(
                "  Hit pagination limit (%d pages)%s — results may be incomplete.",
                MAX_GRAPHQL_PAGES,
                f" for {log_context}" if log_context else "",
            )

        return results

    def node_to_pr_with_review(self, node: dict) -> PRWithReview:
        """Convert a GraphQL PR node to PRWithReview with review metadata."""
        pr, decision, count = self._parse_pr_node(node)
        return PRWithReview(
            pr=pr, review_decision=decision, changes_requested_count=count
        )

    def get_prs_by_user(self, username: str, since: str) -> list[PRWithReview]:
        """Return all PRs by username across self._repos updated since the given
        ISO-8601 cutoff timestamp."""
        author = sanitize_identifier(username, "username")

        search_query = f"{self._repo_qualifier} author:{author} is:pr updated:>={since}"

        results = self._paginated_search(
            self._SEARCH_QUERY, search_query, self.node_to_pr_with_review
        )

        log.info("  GitHub: found %d PR(s) for @%s", len(results), author)
        return results

    def get_prs_by_filter(
        self,
        authors: list[str] | None = None,
        labels: list[str] | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        is_open: bool | None = None,
        is_draft: bool | None = None,
        is_merged: bool | None = None,
    ) -> list[PRWithReview]:
        """Return PRs across self._repos matching all given filters.

        Public library API for external consumers. None leaves a dimension
        unfiltered; multiple authors are OR'd, multiple labels are AND'd.
        """
        query_parts = [self._repo_qualifier, "is:pr"]

        for author in authors or []:
            query_parts.append(f"author:{sanitize_identifier(author, 'author')}")

        for label in labels or []:
            if " " in label or ":" in label:
                query_parts.append(f'label:"{label}"')
            else:
                query_parts.append(f"label:{label}")

        if is_open is not None:
            query_parts.append("is:open" if is_open else "is:closed")
        if is_draft is not None:
            query_parts.append("draft:true" if is_draft else "draft:false")
        if is_merged is not None:
            query_parts.append("is:merged" if is_merged else "is:unmerged")

        if created_after and created_before:
            query_parts.append(
                f"created:{created_after.strftime('%Y-%m-%d')}"
                f"..{created_before.strftime('%Y-%m-%d')}"
            )
        elif created_after:
            query_parts.append(
                f"created:>={created_after.strftime('%Y-%m-%dT%H:%M:%S')}"
            )
        elif created_before:
            query_parts.append(
                f"created:<={created_before.strftime('%Y-%m-%dT%H:%M:%S')}"
            )

        results = self._paginated_search(
            self._SEARCH_QUERY, " ".join(query_parts), self.node_to_pr_with_review
        )

        log.info("  GitHub: found %d PR(s) with filters", len(results))
        return results

    _REVIEW_ACTIVITY_QUERY: Final[str] = """
    query($query: String!, $after: String, $reviewer: String!) {
      search(query: $query, type: ISSUE, first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          ... on PullRequest {
            number
            url
            author { login }
            reviews(first: 50, author: $reviewer) {
              nodes { state submittedAt comments { totalCount } }
            }
          }
        }
      }
    }
    """

    def get_review_activity(self, username: str, since: str) -> list[dict]:
        """PRs the user reviewed (but did not author) updated since the given ISO date.

        Returns raw dicts: {pr_url, pr_author, reviews: [{state, submitted_at,
        comment_count}]}. submitted_at is normalized to +00:00 offset ISO form;
        callers filtering reviews to the window must compare datetime objects,
        not strings.
        """
        reviewer = sanitize_identifier(username, "username")
        since_date = since[:10]

        search_query = (
            f"{self._repo_qualifier} is:pr reviewed-by:{reviewer} "
            f"-author:{reviewer} updated:>={since_date} sort:updated-desc"
        )

        def process_node(node: dict) -> dict:
            return {
                "pr_url": node["url"],
                "pr_author": (node.get("author") or {}).get("login", ""),
                "reviews": [
                    {
                        "state": r.get("state", ""),
                        "submitted_at": _normalize_utc_timestamp(r.get("submittedAt")),
                        "comment_count": (r.get("comments") or {}).get("totalCount", 0),
                    }
                    for r in (node.get("reviews") or {}).get("nodes", [])
                    if r
                ],
            }

        results = self._paginated_search(
            self._REVIEW_ACTIVITY_QUERY,
            search_query,
            process_node,
            extra_variables={"reviewer": reviewer},
            log_context=f"@{reviewer} review activity",
        )

        log.info("  GitHub: found %d reviewed PR(s) for @%s", len(results), reviewer)
        return results

    _ISSUE_SEARCH_QUERY: Final[str] = """
    query($query: String!, $after: String) {
      search(query: $query, type: ISSUE, first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          ... on Issue {
            number
            title
            url
            state
            body
            comments(last: 20) {
              nodes {
                body
                author { login }
                createdAt
              }
            }
            timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT, REFERENCED_EVENT]) {
              nodes {
                __typename
                ... on CrossReferencedEvent {
                  createdAt
                  isCrossRepository
                  source {
                    __typename
                    ... on PullRequest {
                      number
                    }
                    ... on Issue {
                      number
                    }
                  }
                }
                ... on ReferencedEvent {
                  createdAt
                  isCrossRepository
                  commit {
                    oid
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    def _check_issue_mentions_via_search(self, issue_number: int) -> bool:
        """REST Search API check for PRs mentioning an issue number; catches
        mentions from PRs targeting non-default branches that don't create
        CrossReferencedEvents in the timeline API."""

        # GitHub Search API: 30 req/min = 1 req per 2 seconds
        sleep(2.0)

        search_query = f"{self._repo_qualifier} type:pr {issue_number}"

        try:
            resp = self._request(
                "GET",
                f"{self._api_base}/search/issues",
                params={"q": search_query, "per_page": 1},
                headers={"Accept": "application/vnd.github+json"},
            )
            result = resp.json()
            total_count = result.get("total_count", 0)

            if total_count > 0:
                log.debug(
                    "  Search API found %d PR(s) mentioning issue #%d",
                    total_count,
                    issue_number,
                )

            return total_count > 0
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning(
                "  Search API check failed for issue #%d: %s",
                issue_number,
                exc,
            )
            # On error, assume no references (fail open)
            return False

    def get_issues(
        self,
        labels: list[str] | None = None,
        is_open: bool | None = None,
        has_linked_pr: bool | None = None,
        is_available: bool = False,
        comment_only: bool = True,
        github_username: str | None = None,
        since_hours: int | None = None,
    ) -> list[GitHubIssue]:
        """Get issues with optional label/state/linkage/commenter/window filters."""
        query_parts = [self._repo_qualifier, "is:issue"]

        if is_open is not None:
            query_parts.append("is:open" if is_open else "is:closed")

        if labels:
            query_parts.extend(f'label:"{label}"' for label in labels)

        if is_available and has_linked_pr is not False:
            log.warning("Find available prs requires has_linked_pr to be False.")
            has_linked_pr = False

        if has_linked_pr is not None:
            query_parts.append("linked:pr" if has_linked_pr else "-linked:pr")

        username = None
        if github_username:
            username = sanitize_identifier(github_username, "github_username")
            query_parts.append(f"commenter:{username}")

        if since_hours is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
            cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
            query_parts.append(f"updated:>={cutoff_str}")

        search_query = " ".join(query_parts)

        def process_node(node: dict) -> GitHubIssue | None:
            timeline_items = node.get("timelineItems", {}).get("nodes", [])
            latest_comment = ""
            if username:
                comments = node.get("comments", {}).get("nodes", [])
                for c in reversed(comments):
                    author = (c.get("author") or {}).get("login", "")
                    if author.lower() == username.lower():
                        latest_comment = c.get("body", "")
                        break

                if not latest_comment and comment_only:
                    return None

            if is_available:
                for item in timeline_items:
                    event_type = item.get("__typename")
                    if event_type == "CrossReferencedEvent":
                        source_type = item.get("source", {}).get("__typename")
                        if source_type == "PullRequest":
                            return None

                    elif event_type == "ReferencedEvent":
                        if item.get("commit"):
                            return None

                # Fallback: Search API for text mentions (rate limited, 30 req/min)
                if self._check_issue_mentions_via_search(node["number"]):
                    return None

            return GitHubIssue(
                number=node["number"],
                title=node["title"],
                url=node["url"],
                body=(node.get("body") or "")[:MAX_PR_BODY_CHARS],
                state=node.get("state", "OPEN").lower(),
                latest_comment=latest_comment,
            )

        results = self._paginated_search(
            self._ISSUE_SEARCH_QUERY,
            search_query,
            process_node,
        )

        if username:
            log.info(
                "GitHub: found %d issue(s) with comments from @%s",
                len(results),
                username,
            )
        else:
            log.info("  GitHub: found %d issue(s)", len(results))

        return results

    def _parse_pr_node(self, node: dict) -> tuple[PullRequest, ReviewDecision, int]:
        """Parse a single GraphQL PullRequest node into our data models."""
        state, merged = _state_from_gql(node["state"])

        graphql_linked = [
            LinkedIssue(
                number=i["number"],
                title=i["title"],
                url=i["url"],
                body=(i.get("body") or "")[:MAX_PR_BODY_CHARS],
            )
            for i in node.get("closingIssuesReferences", {}).get("nodes", [])
            if i and "number" in i
        ]
        known = {li.number for li in graphql_linked}

        extra_numbers = [
            n for n in _parse_closing_refs(node.get("body") or "") if n not in known
        ]
        pr_repo = repo_from_github_url(node["url"])
        extras = [li for n in extra_numbers if (li := self._fetch_issue(pr_repo, n))]
        linked = tuple(graphql_linked + extras)

        pr = PullRequest(
            number=node["number"],
            title=node["title"],
            url=node["url"],
            state=state,
            merged=merged,
            draft=node.get("isDraft", False),
            updated_at=node["updatedAt"],
            last_human_activity_at=_last_human_activity(
                node, self._ignore_activity_authors
            ),
            body=(node.get("body") or "")[:MAX_PR_BODY_CHARS],
            author=(node.get("author") or {}).get("login", ""),
            linked_issues=linked,
            labels=_labels_from_node(node),
            commit_authors=_commit_author_logins(node, self._ignore_activity_authors),
            merge_labels=self._merge_labels,
        )

        decision, count = GitHubClient._parse_reviews(node, pr.is_active)
        return pr, decision, count

    def _fetch_issue(self, repo: str, number: int) -> LinkedIssue | None:
        """Fetch title/body for an issue found via regex but not GraphQL."""
        try:
            resp = self._request(
                "GET",
                f"{self._api_base}/repos/{repo}/issues/{number}",
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning(
                "  Failed to fetch issue #%d (%s) -- skipping regex match",
                number,
                exc,
            )
            return None
        return LinkedIssue(
            number=data["number"],
            title=data.get("title", ""),
            url=data.get("html_url", f"https://github.com/{repo}/issues/{number}"),
            body=(data.get("body") or "")[:MAX_PR_BODY_CHARS],
        )

    @staticmethod
    def _parse_reviews(node: dict, is_active: bool) -> tuple[ReviewDecision, int]:
        """Extract review decision from GraphQL node."""
        if not is_active:
            return ReviewDecision.NONE, 0

        reviews = node.get("reviews", {}).get("nodes", [])
        latest_by_reviewer: dict[str, str] = {}
        for review in reviews:
            reviewer = (review.get("author") or {}).get("login", "unknown")
            review_state = review.get("state", "")
            if review_state not in ("COMMENTED", "DISMISSED"):
                latest_by_reviewer[reviewer] = review_state

        changes_count = sum(
            1 for s in latest_by_reviewer.values() if s == "CHANGES_REQUESTED"
        )

        gql_decision = node.get("reviewDecision")
        if gql_decision == "CHANGES_REQUESTED":
            return ReviewDecision.CHANGES_REQUESTED, changes_count
        if gql_decision == "APPROVED":
            return ReviewDecision.APPROVED, 0
        if gql_decision == "REVIEW_REQUIRED":
            return ReviewDecision.PENDING, 0
        if reviews:
            return ReviewDecision.PENDING, 0
        return ReviewDecision.NONE, 0

    _PR_BY_NUMBER_QUERY: Final[str] = (
        """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
          number
          title
          url
          state
          merged
          isDraft
          updatedAt
          labels(first: 20) { nodes { name } }
"""
        + _PR_ACTIVITY_FIELDS
        + """
        }
      }
    }
    """
    )

    def get_pr(self, repo: str, pr_number: int) -> PullRequest | None:
        """Fetch one PR's current state (no reviews / linked issues). None if missing."""
        owner, _, name = repo.partition("/")
        data = self._graphql(
            self._PR_BY_NUMBER_QUERY,
            {"owner": owner, "name": name, "number": pr_number},
        )
        node = (data.get("repository") or {}).get("pullRequest")
        if not node:
            return None
        state, merged = _state_from_gql(node["state"])
        return PullRequest(
            number=node["number"],
            title=node["title"],
            url=node["url"],
            state=state,
            merged=merged,
            draft=node.get("isDraft", False),
            updated_at=node["updatedAt"],
            last_human_activity_at=_last_human_activity(
                node, self._ignore_activity_authors
            ),
            labels=_labels_from_node(node),
            commit_authors=_commit_author_logins(node, self._ignore_activity_authors),
            merge_labels=self._merge_labels,
        )

    _ISSUE_COMPETING_PR_QUERY: Final[str] = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        issue(number: $number) {
          timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
            nodes {
              ... on CrossReferencedEvent {
                willCloseTarget
                source {
                  __typename
                  ... on PullRequest {
                    state
                    author { login }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    def issue_has_competing_pr(
        self, repo: str, issue_number: int, claimer_login: str
    ) -> bool:
        """True if the issue has a PR (open or merged) that will close it, authored by
        someone other than claimer_login -- i.e. another contributor took the issue, so
        the claimer's tracking ticket can be retired. Fail-safe False on any error."""
        owner, _, name = repo.partition("/")
        try:
            data = self._graphql(
                self._ISSUE_COMPETING_PR_QUERY,
                {"owner": owner, "name": name, "number": issue_number},
            )
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            log.warning(
                "  Competing-PR check failed for %s#%d (%s)", repo, issue_number, exc
            )
            return False
        issue = (data.get("repository") or {}).get("issue")
        if not issue:
            return False
        claimer = (claimer_login or "").lower()
        for item in issue.get("timelineItems", {}).get("nodes", []):
            if not item.get("willCloseTarget"):
                continue
            src = item.get("source") or {}
            if src.get("__typename") != "PullRequest" or src.get("state") == "CLOSED":
                continue  # not a PR, or an abandoned (closed-unmerged) competitor
            author = ((src.get("author") or {}).get("login") or "").lower()
            if author and author != claimer:
                return True
        return False

    def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Fetch the unified diff for a PR."""
        url = f"{self._api_base}/repos/{repo}/pulls/{pr_number}"
        resp = self._request(
            "GET",
            url,
            headers={"Accept": "application/vnd.github.v3.diff"},
        )
        return resp.text

    def get_issue_comments(
        self, repo: str, issue_number: int, max_comments: int = 10
    ) -> list[dict[str, Any]]:
        """Fetch recent comments from a GitHub issue.

        Returns list of dicts with: author, created_at, body.
        Sorted by creation time (oldest first). Limited to max_comments.
        """
        try:
            url = f"{self._api_base}/repos/{repo}/issues/{issue_number}/comments"
            resp = self._request(
                "GET",
                url,
                params={
                    "sort": "created",
                    "direction": "desc",
                    "per_page": max_comments,
                },
            )
            data = resp.json()
            # Reverse to get chronological order (oldest first)
            return [
                {
                    "author": (c.get("user") or {}).get("login", "unknown"),
                    "created_at": c.get("created_at", ""),
                    "body": c.get("body", "")[:2000],  # Limit comment body
                }
                for c in reversed(data) if c
            ]
        except (requests.RequestException, ValueError) as exc:
            log.warning(
                "  Failed to fetch comments for %s#%d (%s)", repo, issue_number, exc
            )
            return []

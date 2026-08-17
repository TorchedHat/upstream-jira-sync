from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Final
from urllib.parse import urlsplit, urlunsplit

MAX_GRAPHQL_PAGES: Final[int] = 20
MAX_PR_BODY_CHARS: Final[int] = 600
DEFAULT_CONNECT_TIMEOUT: Final[int] = 10
DEFAULT_READ_TIMEOUT: Final[int] = 30
STATE_TTL_DAYS: Final[int] = 90

JIRA_NO_NOTIFY: Final[dict[str, str]] = {"notifyUsers": "false"}

VALID_STORY_POINTS: Final[tuple[int, ...]] = (1, 2, 3, 5, 8, 13)

MAX_ISSUE_BODY_CHARS: Final[int] = 2000
MAX_COMMENT_CHARS: Final[int] = 1000
MAX_TICKET_DESC_CHARS: Final[int] = 600

_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")


def sanitize_identifier(value: str, field_name: str) -> str:
    """Validate that a value is safe to interpolate into API queries."""
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid characters in {field_name}: {value!r}. "
            f"Only alphanumeric, '.', '_', '@', '-' are allowed."
        )
    return value


def repo_from_github_url(url: str) -> str:
    """Extract 'owner/repo' from a github.com URL like .../owner/repo/pull/N."""
    parts = url.split("/")
    if len(parts) < 5 or "github.com" not in parts[2]:
        raise ValueError(f"Cannot parse owner/repo from URL: {url}")
    return f"{parts[3]}/{parts[4]}"


def canonical_github_url(url: str) -> str:
    """Lowercase host, strip trailing slash/query/fragment; /issues/ vs /pull/ preserved."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, host, path, "", ""))


def pr_number_from_github_url(url: str) -> int | None:
    parts = urlsplit(url).path.strip("/").split("/")
    if "pull" in parts:
        i = parts.index("pull")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            return int(parts[i + 1])
    return None


def issue_number_from_github_url(url: str) -> int | None:
    parts = urlsplit(url).path.strip("/").split("/")
    if "issues" in parts:
        i = parts.index("issues")
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            return int(parts[i + 1])
    return None


class ReviewDecision(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    PENDING = "PENDING"
    NONE = "NONE"


class CanonicalStatus(str, Enum):
    """Workflow-independent states; config.status_map resolves each to the
    Jira instance's real status name (R4)."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    DONE = "done"


class PROutcome(str, Enum):
    DRAFT = "draft"
    IGNORED_LABEL = "ignored_label"
    BOT_ACTIVITY = "bot_activity"
    NO_TICKETS = "no_tickets"
    LOW_CONF = "low_conf"
    MATCHED = "matched"


class PRLifecycleState(str, Enum):
    OPEN = "OPEN"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    MERGED = "MERGED"
    CLOSED_UNMERGED = "CLOSED_UNMERGED"


@dataclass(frozen=True)
class SprintRef:
    id: int
    name: str


@dataclass(frozen=True)
class LinkedIssue:
    number: int
    title: str
    url: str
    body: str = ""


@dataclass(frozen=True)
class PullRequest:
    """Immutable snapshot of a GitHub pull request.

    merge_labels is stamped at construction by GitHubClient from
    config.merge_labels (R6): labels a merge bot applies when it merges by
    closing. Default empty — plain closed PRs count as cancelled.
    """

    number: int
    title: str
    url: str
    state: str  # "open" | "closed"
    merged: bool
    draft: bool
    updated_at: str  # ISO-8601 string
    # Latest activity not attributable to a bot (commit, close, non-bot comment or
    # review). Empty when unknown -- callers fall back to updated_at.
    last_human_activity_at: str = ""
    body: str = ""
    author: str = ""  # GitHub username
    linked_issues: tuple[LinkedIssue, ...] = ()
    labels: tuple[str, ...] = ()
    # GitHub logins that authored commits on the PR, including Co-authored-by
    # credits; bots excluded upstream.
    commit_authors: tuple[str, ...] = ()
    merge_labels: tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        return self.state == "open" and not self.draft

    @property
    def effectively_merged(self) -> bool:
        if self.merged:
            return True
        return self.state == "closed" and self.has_any_label(self.merge_labels)

    @property
    def is_cancelled(self) -> bool:
        return self.state == "closed" and not self.effectively_merged

    def has_any_label(self, names: tuple[str, ...] | list[str] | None) -> bool:
        present = {label.lower() for label in self.labels}
        return any(name.lower() in present for name in (names or ()))

    @property
    def status_icon(self) -> str:
        if self.effectively_merged:
            return "[merged]"
        if self.state == "closed":
            return "[closed]"
        return "[open]"

    @property
    def updated_date(self) -> str:
        return self.updated_at[:10]

    @property
    def human_activity_at(self) -> str:
        """last_human_activity_at, falling back to updated_at when unknown."""
        return self.last_human_activity_at or self.updated_at


@dataclass(frozen=True)
class PRWithReview:
    pr: PullRequest
    review_decision: ReviewDecision
    changes_requested_count: int


@dataclass
class JiraTicket:
    key: str
    summary: str
    status: str
    url: str
    remote_links: list[str] = field(default_factory=list)
    description: str = ""
    labels: list[str] = field(default_factory=list)
    parent_key: str = ""
    issuetype: str = ""
    sprint_ids: set[int] = field(default_factory=set)
    team_id: str = ""

    def is_status(self, status_name: str) -> bool:
        return self.status.lower() == status_name.lower()

    def is_type(self, issue_type: str) -> bool:
        """Compare issuetype by name; callers pass config.issue_type or
        config.container_issue_type (R5) — never a hardcoded literal."""
        return self.issuetype.lower() == issue_type.lower()

    @property
    def pr_links(self) -> list[str]:
        return [u for u in self.remote_links if "/pull/" in u]

    @property
    def issue_links(self) -> list[str]:
        return [u for u in self.remote_links if "/issues/" in u]

    def __str__(self) -> str:
        return f"{self.key} [{self.status}] — {self.summary[:60]}"


@dataclass(frozen=True)
class JiraTicketChange:
    ticket_key: str
    ticket_summary: str
    field: str
    from_value: str
    to_value: str
    changed_at: str
    author_email: str
    ticket_assignee_email: str = ""


@dataclass(frozen=True)
class TeamMember:
    """Maps a GitHub username to a Jira assignee email.

    __str__ deliberately omits jira_email: log lines reference GitHub
    handles only (R10).
    """

    github: str
    jira_email: str

    def __str__(self) -> str:
        return f"@{self.github}"


@dataclass(frozen=True)
class GitHubIssue:
    number: int
    title: str
    url: str
    body: str
    state: str  # "open" | "closed"
    latest_comment: str = ""


@dataclass(frozen=True)
class ClaimResult:
    intent: str  # "claiming" | "not_claiming"
    reason: str


@dataclass(frozen=True)
class MatchResult:
    ticket: JiraTicket
    confidence: str
    reason: str


@dataclass
class SyncSummary:
    """Accumulates counters across an entire sync run.

    Invariant (when errors == 0):
        seen == draft + ignored_label + bot_activity + no_tickets + low_conf + matched.
    """

    seen: int = 0
    draft: int = 0
    ignored_label: int = 0
    bot_activity: int = 0
    no_tickets: int = 0
    low_conf: int = 0
    matched: int = 0
    transitioned: int = 0
    commented: int = 0
    comment_dedup: int = 0
    estimated: int = 0
    would_create: int = 0
    issues_created: int = 0
    pr_tickets_created: int = 0
    stale_closed: int = 0
    claim_superseded_closed: int = 0
    cancelled_closed: int = 0
    pr_orphaned: int = 0
    sprint_swept: int = 0
    sprint_provisioned: int = 0
    team_assigned: int = 0
    co_authors_noted: int = 0
    release_tagged: int = 0
    errors: int = 0

    def record_pr_outcome(self, outcome: PROutcome) -> None:
        self.seen += 1
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)

    def __str__(self) -> str:
        return (
            f"seen={self.seen} draft={self.draft} "
            f"ignored_label={self.ignored_label} bot_activity={self.bot_activity} "
            f"no_tickets={self.no_tickets} "
            f"low_conf={self.low_conf} matched={self.matched} "
            f"transitioned={self.transitioned} commented={self.commented} "
            f"comment_dedup={self.comment_dedup} estimated={self.estimated} "
            f"would_create={self.would_create} issues_created={self.issues_created} "
            f"pr_tickets_created={self.pr_tickets_created} stale_closed={self.stale_closed} "
            f"claim_superseded_closed={self.claim_superseded_closed} "
            f"cancelled_closed={self.cancelled_closed} "
            f"pr_orphaned={self.pr_orphaned} sprint_swept={self.sprint_swept} "
            f"sprint_provisioned={self.sprint_provisioned} "
            f"team_assigned={self.team_assigned} "
            f"co_authors_noted={self.co_authors_noted} "
            f"release_tagged={self.release_tagged} errors={self.errors}"
        )

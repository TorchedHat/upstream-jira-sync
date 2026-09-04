# upstream-jira-sync porting contracts

Binding specification for porting the reference repo at
`/myworkspace/gh-jira-sync` (READ-ONLY) into this framework. Below, `OLD/` means
that repo's inner Python package (the one containing `sync/`) — always name it
by path, never by its package name, so no tenant literals leak into this repo.
Porters implement EXACTLY these signatures. Where a section says "faithful
port", copy the old module's logic verbatim except for the listed changes and
the mechanical import-path rewrites below.

## Package layout

```
upstream_jira_sync/
  __init__.py        version string (done)
  models.py          shared domain dataclasses/enums/constants (done)
  config.py          AppConfig + TeamSpec + LLMSettings (done, source of truth)
  http.py            BaseHTTPClient
  github.py          GitHubClient
  adf.py             AdfBuilder, adf_to_text
  jira.py            JiraClient, DryRunJiraClient
  resolver.py        StatusResolver, derive_lifecycle_state
  state.py           SyncState (schema v1, see below)
  teams.py           taxonomy helpers over config.teams
  sprint.py          pure sprint date math
  tagging.py         TicketTagger
  rfc_epics.py       RfcEpicTracker
  override_gate.py   ManualOverrideGate, story_points_blocked, status_blocked
  ai.py              LLM-backed classifiers/estimators
  skill_loader.py    SkillLoader (packaged defaults + validated overrides);
                     named skill_loader.py because skills/ is the prompt dir
  orchestrator.py    SyncOrchestrator
  cli.py             console entry: sync / check-config subcommands
  digest.py          weekly digest -> GitHub Discussion
  emailer.py         GmailNotifier
  review_activity.py review stats for the digest
  llm/
    __init__.py      re-exports (done)
    base.py          LLMProvider protocol + load_provider (done)
    vertex.py        VertexProvider
    anthropic.py     AnthropicProvider
  skills/*.md        packaged default prompts (see registry)
```

**Design decision:** shared non-config dataclasses/enums (`PullRequest`,
`JiraTicket`, `SyncSummary`, `CanonicalStatus`, URL helpers, size constants)
live in `upstream_jira_sync/models.py`; `config.py` holds only configuration
(`AppConfig`, `TeamSpec`, `LLMSettings`) and imports `TeamMember`/
`CanonicalStatus` from models. Nothing imports config from models, so there are
no cycles.

## Global mechanical rewrites (apply to every port)

- `OLD/sync/X.py` imports -> `upstream_jira_sync.X`; `OLD/X.py` (digest, emailer,
  review_activity) -> `upstream_jira_sync.X`. Rewrite every old-package import to the
  `upstream_jira_sync` package name.
- `from ...models import AppConfig` -> `from upstream_jira_sync.config import AppConfig`.
- `JiraStatus` -> `CanonicalStatus` (values are `todo/in_progress/review/done`;
  the instance's real names come from `config.status_map`, R4).
- `_sanitize_identifier` -> public `sanitize_identifier`; `_JIRA_NO_NOTIFY` -> `JIRA_NO_NOTIFY`.
- No tenant literals anywhere (R3). No `Final` team dicts, no hardcoded
  customfield ids, no hardcoded merge labels, no product names in prompts/docstrings.
- Style: match old idiom; minimal comments; modules under ~500 LOC (R14).
- Never log `jira_email` values; log GitHub handles only (R10). `TeamMember.__str__`
  is already `@{github}`-only — do not "fix" it back.

---

## Module contracts

### upstream_jira_sync/http.py  (from sync/http.py, 69 LOC)
Faithful port, zero behavior change.
```python
class BaseHTTPClient:
    _MAX_RETRIES: Final[int] = 4
    _BASE_DELAY: Final[int] = 2
    def __init__(self) -> None
    def __enter__(self) -> BaseHTTPClient
    def __exit__(self, *_: Any) -> None
    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response

def _parse_retry_after(header_value: str | None, default: int) -> int
```
Changes: imports `DEFAULT_CONNECT_TIMEOUT`/`DEFAULT_READ_TIMEOUT` from
`upstream_jira_sync.models`. Nothing else.

### upstream_jira_sync/adf.py  (from sync/adf.py, 152 LOC)
```python
def adf_to_text(adf: Any) -> str

class AdfBuilder:
    @staticmethod
    def pr_comment(pr: PullRequest, status_name: str, *,
                   match_confidence: str = "", match_reason: str = "",
                   note: str = "") -> dict
    @staticmethod
    def note(text: str) -> dict
    @staticmethod
    def issue_description(issue_title: str, issue_url: str, summary: str) -> dict
    @staticmethod
    def pr_description(pr_title: str, pr_url: str, summary: str) -> dict
```
Changes: `pr_comment` takes the resolved status **name string** instead of the
old `JiraStatus` enum (R4) — callers pass `config.status_name(target)`.
Everything else faithful.

### upstream_jira_sync/github.py  (from sync/github.py, 780 LOC — read in chunks, R16)
```python
class GitHubClient(BaseHTTPClient):
    def __init__(self, token: str, repos: list[str],
                 merge_labels: tuple[str, ...] = (),
                 base_url: str = "") -> None
        # base_url overrides https://api.github.com (mock-server routing only)
    def node_to_pr_with_review(self, node: dict) -> PRWithReview
    def get_prs_by_user(self, username: str, since: str) -> list[PRWithReview]
    def get_review_activity(self, username: str, since: str) -> list[dict]
    def get_issues(...)                     # same signature as old
    def get_pr(self, repo: str, pr_number: int) -> PullRequest | None
    def issue_has_competing_pr(...)         # same signature as old
    def get_pr_diff(self, repo: str, pr_number: int) -> str
```
Changes:
- Constructor gains `merge_labels` (from `config.merge_labels`, R6) and stamps
  it onto every `PullRequest` it constructs (`merge_labels=self._merge_labels`).
  The module-level `MERGE_LABELS` constant is gone; `effectively_merged` now
  reads the instance field.
- Module helpers (`_normalize_utc_timestamp`, `_state_from_gql`,
  `_labels_from_node`, `_strip_non_prose`, `_parse_closing_refs`) are faithful.
- All GraphQL queries faithful.

### upstream_jira_sync/jira.py  (from sync/jira.py, 766 LOC — read in chunks, R16)
```python
RFC_EPIC_GLOBAL_ID_PREFIX = "rfc-epic::"

def _jql_escape(value: str) -> str

class JiraClient(BaseHTTPClient):
    def __init__(self, url: str, email: str, token: str, *,
                 sprint_field: str = "",
                 container_issue_type: str = "Epic",
                 automation_opt_out_labels: tuple[str, ...] = ()) -> None

    def get_open_tickets(self, assignee_email: str,
                         project_key: str | None = None) -> list[JiraTicket]
    def get_open_containers(self, assignee_email: str,
                            project_key: str | None = None) -> list[JiraTicket]
    def get_sprint_sweep_candidates(self, assignee_email: str, project_key: str,
                                    statuses: list[str], since_iso: str) -> list[JiraTicket]
    def find_tracking_ticket(self, issue_url: str, project_key: str) -> JiraTicket | None
    def find_epic_for_rfc(self, rfc_url: str, project_key: str) -> JiraTicket | None
    def find_candidate_tickets(self, project_key: str, title: str,
                               max_results: int = 15) -> list[JiraTicket]
    def search_ticket_changes(self, jql: str, since_iso: str,
                              story_points_field: str,
                              max_results: int = 100) -> list[JiraTicketChange]
    def get_sprint_by_name(self, board_id: int, name: str) -> SprintRef | None
    def add_issues_to_sprint(self, sprint_id: int, issue_keys: list[str]) -> None
    def create_sprint(self, board_id: int, name: str, start: date, end: date) -> SprintRef

    def create_ticket(self, project_key: str, summary: str, description_adf: dict,
                      assignee_email: str, *, labels: list[str] | None = None,
                      extra_fields: dict | None = None, issuetype: str = "Story",
                      parent_key: str | None = None,
                      initial_status_name: str = "To Do") -> JiraTicket
    def set_story_points(self, ticket: JiraTicket, points: int, field_name: str) -> None
    def update_labels(self, ticket_key: str, labels: list[str]) -> None
    def set_team(self, ticket_key: str, team_id: str, team_field: str) -> None
    def set_parent(self, ticket_key: str, parent_key: str) -> None
    def post_comment(self, ticket: JiraTicket, pr: PullRequest, status_name: str,
                     match_confidence: str = "", match_reason: str = "",
                     note: str = "") -> None
    def post_note(self, ticket: JiraTicket, text: str) -> None
    def add_remote_link(self, ticket: JiraTicket, url: str, title: str,
                        relationship: str = "pull request", *, global_id: str = "") -> None
    def transition_ticket(self, ticket: JiraTicket, target_status_name: str) -> bool

class DryRunJiraClient(JiraClient):
    # overrides the same write methods as the old DryRunJiraClient, log-only
```
Changes:
1. **Issue-type clauses config-driven (R5).** The old module-level
   `_EXCLUDE_EPICS = "AND issuetype != Epic"` / `_ONLY_EPICS` constants become
   instance strings built in `__init__` from `container_issue_type`:
   `self._exclude_containers = f'AND issuetype != "{container_issue_type}"'`
   (quoted; sanitize with `_jql_escape`). `get_open_epics` is renamed
   `get_open_containers` and `find_epic_for_rfc` checks
   `ticket.is_type(self._container_issue_type)` instead of `== "Epic"`.
2. **Transitions resolved by TARGET STATUS (R4).** `transition_ticket` takes the
   resolved instance status **name** (`config.status_name(canonical)`). It
   fetches `GET /issue/{key}/transitions?expand=transitions.fields` (plain GET
   as today), then selects the transition whose `t["to"]["name"].lower() ==
   target_status_name.lower()`; if the payload includes ids, prefer matching a
   previously seen `to.id` for the same name (cache `name -> to.id` per run).
   NEVER match on `t["name"]` (the transition's own label). On no match, log
   the available `to.name` values and return False.
3. **`set_team` takes the field id** (`config.team_field`) instead of importing
   a constant from teams.py (R7/R8). Value stays a bare id string.
4. **`create_ticket`** gains `initial_status_name` (callers pass
   `config.status_name(CanonicalStatus.TODO)`) used only for the returned
   ticket's `status` attribute; the old code hardcoded `JiraStatus.TODO.value`.
5. `_sprint_field` has NO default field id (R8); when empty,
   `_search_tickets` omits it from the fields list and leaves `sprint_ids` empty.
6. **`automation_opt_out_labels`** builds `_exclude_opt_out`, applied to
   `get_sprint_sweep_candidates` only. The candidate pools that feed PR-driven
   sync (`get_open_tickets`) deliberately do NOT carry the clause: opting out of
   metadata writes must not make a ticket invisible to PR matching. The clause
   is `(labels IS EMPTY OR labels NOT IN (...))` — the empty arm is load-bearing
   because a bare JQL `NOT IN` drops issues whose field is empty.
7. Everything else (JQL shapes, remote-link backfill, changelog parsing,
   `notifyUsers=false` on all writes) is faithful.

### upstream_jira_sync/resolver.py  (from sync/resolver.py, 61 LOC)
```python
def derive_lifecycle_state(pr: PullRequest, review_decision: str | None,
                           changes_requested_count: int) -> PRLifecycleState

class StatusResolver:
    def __init__(self, significant_comments_threshold: int) -> None
    def resolve(self, pr: PullRequest, review_decision: ReviewDecision,
                changes_requested_count: int) -> CanonicalStatus
```
Changes: returns `CanonicalStatus` (`DONE` where the old code said
`JiraStatus.CLOSED`, `REVIEW`, `IN_PROGRESS`) — R4. Logic otherwise faithful.

### upstream_jira_sync/state.py  (from sync/state.py, 384 LOC)
```python
SCHEMA_VERSION = 1

class SyncState:
    def __init__(self, path: str = "sync_state.json", read_only: bool = False) -> None
    # identical public surface to the old SyncState:
    def is_commented / record_comment
    def is_estimated / record_estimation
    def is_pr_tracked / get_tracked_ticket_key / record_pr_orphaned / record_pr_tracked
    def get_rfc_epic / record_rfc_epic
    def is_low_conf_pinged / record_low_conf_ping
    def record_pr_cancel_seen / clear_pr_cancel_seen / record_cancel_commented
    def is_issue_processed / record_issue_classification / set_issue_ticket
    def get_pr_state_snapshot / record_pr_state_snapshot
    # new (R11):
    def record_digest_event(self, kind: str, *, ticket_key: str = "",
                            pr_url: str = "", issue_url: str = "",
                            github_user: str = "", old_value: str = "",
                            new_value: str = "") -> None
    def read_digest_events(self, since_iso: str) -> list[dict]
```
Changes (R11):
- Fresh `SCHEMA_VERSION = 1`; no migration from the old bot's file (new state
  files start empty). Drop the old `_migrate` v1/v2 paths.
- Namespaces: `comments, estimations, classifications, tracking, locks,
  rfc_tracking, low_conf_pings, pr_state_snapshots, digest`.
- **Core dedup entries must NOT store `github_user`** — remove it from
  `record_comment`, `record_estimation`, `record_issue_classification`,
  `record_pr_tracked` signatures/payloads (the old code stored it for digest use).
- Per-person attribution lives ONLY in the `digest` namespace via
  `record_digest_event`, and the orchestrator calls it ONLY when
  `config.digest_enabled` is true. `digest` entries carry `observed_at`
  timestamps and are TTL-pruned like every namespace (90 days).

**State-file schema (v1):**
```jsonc
{
  "version": 1,
  "comments":        { "<pr_url>::<ticket_key>": {"commented_at": iso, "last_status": str} },
  "estimations":     { "<pr_url>::<ticket_key>": {"estimated_at": iso, "story_points": int} },
  "classifications": { "<issue_url>": {"classified_at": iso, "intent": str, "ticket_key": str} },
  "tracking":        { "<pr_url>": {"tracked_at": iso, "ticket_key": str, "orphaned_at": iso?} },
  "locks":           { "<pr_url>::<ticket_key>::cancel": {"locked_at": iso, ...} },
  "rfc_tracking":    { "<rfc_url>": {"tracked_at": iso, "epic_key": str} },
  "low_conf_pings":  { "<pr_url>": {"pinged_at": iso} },
  "pr_state_snapshots": { "<ticket_key>": {"observed_at": iso, "lifecycle": str, ...} },
  "digest": {                                    // ONLY namespace with attribution (R11)
    "events": [ {"observed_at": iso, "kind": str, "ticket_key": str,
                 "pr_url": str, "issue_url": str, "github_user": str,
                 "old_value": str, "new_value": str} ]
  }
}
```

### upstream_jira_sync/teams.py  (replaces sync/teams.py, R7)
Pure helpers over `config.teams`; NO module-level team data.
```python
def teams_to_labels(teams: Sequence[TeamSpec], names: Iterable[str]) -> list[str]
    # sorted labels for the given canonical names, unknown names dropped
def primary_team_id(teams: Sequence[TeamSpec], names: list[str]) -> str | None
    # team_id of the first classified name, or None (also when team_id unset)
def canonical_team_names(teams: Sequence[TeamSpec]) -> frozenset[str]
def render_team_prompt_section(teams: Sequence[TeamSpec]) -> str
    # deterministic bullet list of team names injected into the
    # team_classification prompt as {teams_section} (R7); names only —
    # scope/keyword guidance belongs in a team's skills_dir override
```

### upstream_jira_sync/sprint.py  (from sync/sprint.py, 46 LOC)
```python
def current_sprint_number(anchor: date, today: date, anchor_number: int,
                          sprint_days: int = 14) -> int | None
def sprint_window(anchor: date, anchor_number: int, number: int,
                  sprint_days: int = 14) -> tuple[date, date]
def sprints_to_provision(anchor: date, today: date, anchor_number: int,
                         lookahead: int, sprint_days: int = 14) -> list[int]
def sweep_cutoff_date(anchor: date, today: date, lookback_sprints: int,
                      sprint_days: int = 14) -> date | None
```
Changes: the hardcoded `SPRINT_DAYS = 14` becomes a parameter fed from
`config.sprint_length_days`. Math otherwise faithful.

### upstream_jira_sync/tagging.py  (from sync/tagging.py, 326 LOC)
```python
class TicketTagger:
    def __init__(self, config: AppConfig, github: GitHubClient, jira: JiraClient,
                 team_classifier: TeamClassifier | None = None) -> None
    def resolve_current_sprint(self) -> SprintRef | None
    def get_pr_files(self, repo: str, pr_number: int, pr_url: str) -> tuple[str, ...]
    def ordered_teams_for_pr(self, pr: PullRequest) -> list[str]
    def team_assignment_for_pr(self, pr: PullRequest) -> tuple[list[str], str | None]
    def backfill_team_labels(self, member: TeamMember, tickets: list[JiraTicket],
                             summary: SyncSummary) -> None
    def sweep_sprint(...)                    # same shape as old
    def provision_future_sprints(self, summary: SyncSummary) -> None
```
Changes: `_opted_out(ticket)` gates `sweep_sprint` and `backfill_team_labels`
against `config.automation_opt_out_labels` (case-insensitive), so a labelled
card is left alone by both; team lookups go through `upstream_jira_sync.teams`
helpers with `self._config.teams` (R7); sprint math passes `config.sprint_length_days`;
sweep statuses come from `config.active_status_names` /
`config.status_name(...)` (R4). Diff-path parsing, caches, shadow/auto gating
faithful.

### upstream_jira_sync/rfc_epics.py  (from sync/rfc_epics.py, 190 LOC)
```python
RFC_TITLE_RE = re.compile(r"^\s*(\[RFC\]|RFC:)", re.IGNORECASE)

class RfcEpicTracker:
    def __init__(self, config: AppConfig, jira: JiraClient, state: SyncState,
                 github: GitHubClient, summarizer: IssueSummarizer | None = None) -> None
    def ensure_epic(self, rfc_url: str, rfc_title: str, rfc_body: str,
                    member: TeamMember) -> str | None
    def parent_for_pr(self, pr: PullRequest, member: TeamMember) -> str | None
    def maybe_reparent(...)                  # same shape as old
```
Changes: the old module-level `EPIC_NAME_FIELD` constant (a hardcoded custom
field id) is deleted (R3, R8). `ensure_epic` creates with
`issuetype=config.container_issue_type` (R5)
and passes `extra_fields={config.epic_name_field: epic_summary}` ONLY when
`config.epic_name_field` is set; otherwise `extra_fields=None` (R8). Remote-link
global-id namespacing (`rfc-epic::`) faithful.

### upstream_jira_sync/override_gate.py  (from sync/override_gate.py, 191 LOC)
Faithful port.
```python
class ManualOverrideGate:
    def __init__(self, jira: JiraClient, bot_email: str,
                 bot_account_id: str = "", aliases: tuple[str, ...] = ()) -> None
    def prefetch(self, ticket_keys: list[str], field_ids: set[str]) -> None
    def is_human_owned(self, ticket_key: str, field_id: str) -> bool
    def is_unreliable(self, ticket_key: str) -> bool

def story_points_blocked(...)   # same as old
def status_blocked(...)         # same as old
```
Changes: none besides imports; never log author emails at INFO (R10) — key/
field only.

### upstream_jira_sync/ai.py  (from sync/ai.py, 568 LOC)
All classes consume the `LLMProvider` protocol; the model is bound inside the
provider, so `model` disappears from constructors. The CLI resolves the model
per task from `llm.models` (falling back to `llm.model`) and hands each class
the provider for its task, so two classes may hold different providers.
```python
class _SkillBasedAI:
    _SKILL_NAME: str = ""
    def __init__(self, llm: LLMProvider, skill_loader: SkillLoader) -> None
        # self._llm, self._system = skill_loader.load(self._SKILL_NAME)

class AITicketMatcher(_SkillBasedAI):
    def find_best(self, pr: PullRequest, tickets: list[JiraTicket]) -> MatchResult | None

class StoryPointEstimator(_SkillBasedAI):
    def estimate(self, pr: PullRequest, ticket: JiraTicket) -> int | None
    def estimate_from_issue(self, ticket: JiraTicket, issue_title: str,
                            issue_body: str) -> int | None

class IssueSummarizer(_SkillBasedAI):
    def summarize(self, title: str, body: str) -> str

class WeeklyDigestSummarizer(_SkillBasedAI):
    def summarize(self, events_json: str) -> str

class RfcClassifier(_SkillBasedAI):
    def classify(self, title: str, body: str) -> str | None   # 'epic' | 'story' | None

class IssueClaimClassifier(_SkillBasedAI):
    def classify(self, issue: GitHubIssue, comment: str, username: str) -> ClaimResult

class IssueDeduplicator(_SkillBasedAI):
    def find_existing(self, issue_title: str, issue_body: str,
                      tickets: list[JiraTicket]) -> MatchResult | None

class TeamClassifier:
    def __init__(self, llm: LLMProvider, skill_loader: SkillLoader,
                 teams: Sequence[TeamSpec]) -> None
    def classify_ordered(self, pr_title: str, pr_body: str,
                         file_paths: tuple[str, ...]) -> list[str]
    def classify(self, pr_title: str, pr_body: str,
                 file_paths: tuple[str, ...]) -> set[str]
```
Changes:
1. Every `self._anthropic.create_message(model=..., system=..., user_message=...,
   max_tokens=N)` becomes `self._llm.complete(system=..., user_message=...,
   max_tokens=N)` (R9). Same max_tokens values as old (256/128/192/600).
2. `TeamClassifier` takes `teams` and builds its prompt by formatting the
   loaded skill with `{teams_section}` = `render_team_prompt_section(teams)`
   plus `{pr_title}/{pr_body}/{file_paths}` (R7). Valid names come from
   `canonical_team_names(teams)`, not a frozen constant.
3. `RfcClassifier.classify` verdicts stay the strings `'epic' | 'story'`
   (framework-internal canonical words; the container issue type they map to is
   `config.container_issue_type`, applied by the caller — R5).
4. Prompt templates, JSON parsing, confidence gates, logging: faithful.

### upstream_jira_sync/skill_loader.py  (from sync/utils.py SkillLoader + helpers, R13)
```python
def strip_markdown_fences(text: str) -> str          # was _strip_markdown_fences
def is_bot_author(email: str | None, bot_email: str) -> bool
def is_bot_actor(author: dict | None, bot_email: str,
                 bot_account_id: str = "", aliases: tuple[str, ...] = ()) -> bool

SKILL_TEMPLATE_VARS: Final[dict[str, frozenset[str]]] = {
    "ticket_matcher": frozenset(),
    "story_point_estimation": frozenset(),
    "issue_summarizer": frozenset(),
    "issue_claim_classifier": frozenset(),
    "issue_dedup_matcher": frozenset(),
    "rfc_classifier": frozenset(),
    "weekly_digest_summary": frozenset(),
    "review_activity_intro": frozenset(),
    "team_classification": frozenset({"teams_section", "pr_title", "pr_body", "file_paths"}),
    "low_conf_email": frozenset({"github", "pr_number", "pr_title", "pr_url"}),
}

class SkillLoader:
    def __init__(self, override_dir: str = "") -> None
    def load(self, skill_name: str) -> str
```
`load()` resolution order (per file, R13):
1. `{override_dir}/{skill_name}.md` if `override_dir` set and the file exists;
2. packaged default `upstream_jira_sync/skills/{skill_name}.md` via
   `importlib.resources.files("upstream_jira_sync") / "skills"`.

YAML frontmatter stripping and caching faithful. After loading an OVERRIDE,
validate that every variable in `SKILL_TEMPLATE_VARS[skill_name]` appears as a
literal `{var}` in the text; on failure raise
`ValueError("skill override '<name>' is missing template variables: {a, b}")`
(fail fast, R13). Packaged defaults are trusted and not re-validated.

**Packaged default prompts:** port each of the old repo's `skills/*.md` files,
rewriting away every tenant literal (R3): the team table in
`team_classification.md` is replaced by the `{teams_section}` placeholder plus
generic classification rules; product-specific examples become neutral ones;
`low_conf_email.md` keeps its four variables.

### upstream_jira_sync/orchestrator.py  (from sync/orchestrator.py, 1064 LOC — read in chunks, R16)
```python
class SyncOrchestrator:
    def __init__(self, config: AppConfig, github: GitHubClient, jira: JiraClient,
                 matcher: AITicketMatcher, resolver: StatusResolver, state: SyncState,
                 estimator: StoryPointEstimator | None = None,
                 classifier: IssueClaimClassifier | None = None,
                 summarizer: IssueSummarizer | None = None,
                 deduplicator: IssueDeduplicator | None = None, *,
                 override_gate: ManualOverrideGate | None = None,
                 team_classifier: TeamClassifier | None = None,
                 rfc_classifier: RfcClassifier | None = None,
                 emailer: GmailNotifier | None = None) -> None
    def run(self) -> SyncSummary
```
Private method set mirrors the old file one-for-one. Changes:
- All status comparisons/transitions use `CanonicalStatus` +
  `config.status_name(...)`; `_apply_transition(ticket, target: CanonicalStatus)`
  calls `jira.transition_ticket(ticket, self._config.status_name(target))` (R4).
- Epic checks use `ticket.is_type(config.container_issue_type)` (R5);
  ticket creation passes `issuetype=config.issue_type` (or the container type
  in the RFC path).
- Wherever the old code stored `github_user` into state dedup entries, drop it;
  instead, when `config.digest_enabled`, additionally call
  `state.record_digest_event(...)` at the same sites (pr_linked, points set,
  issue claimed, ticket created, pr orphaned) — R11.
- Low-confidence email body renders the `low_conf_email` skill with its four
  variables; recipient resolution unchanged but never logged as an email (R10).
- Everything else (claim flow, dedup flow, cancel debounce, stale sweep,
  superseded-claim close, sprint sweep hooks, team backfill) faithful.

### upstream_jira_sync/cli.py  (from sync/cli.py, 213 LOC — restructured, R15)
```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace
def main(argv: list[str] | None = None) -> None
def run_sync(args: argparse.Namespace) -> int
def run_check_config(args: argparse.Namespace) -> int
```
`upstream-jira-sync` console script with subparsers:
- `upstream-jira-sync sync [--dry-run] [--member GITHUB_USERNAME] [--config PATH]
  [--state-file PATH] [--since-hours N] [--mock-url URL]` — wiring identical in
  spirit to the old `main()`: build provider via
  `load_provider(config.llm)` (setting `config.llm.base_url = args.mock_url`
  first), `SkillLoader(override_dir=config.skills_dir)`, conditional AI
  services per feature flags, `DryRunJiraClient` on `--dry-run`,
  `GitHubClient(token, repos, merge_labels=tuple(config.merge_labels),
  base_url=args.mock_url)` (mock routes GitHub, Jira, and LLM),
  `JiraClient(url, email, token, sprint_field=config.sprint_field,
  container_issue_type=config.container_issue_type)` (wiring sets
  `jira._team_field = config.team_field` when configured, so searches populate
  `ticket.team_id`), optional
  `ManualOverrideGate`, then `SyncOrchestrator(...).run()`; exit 1 when
  `summary.errors > 0`. `--member` filter errors list known GitHub handles
  only (R10).
- `upstream-jira-sync check-config [--config PATH] [--live]` — offline: run
  `AppConfig.load(path, require_runtime=False)` (skips env secrets + roster
  presence; everything else validated) and report OK or the aggregated
  ValueError; with `--live`, load with `require_runtime=True`; also verify
  roster shape and that every `SKILL_TEMPLATE_VARS` skill loads (exercises
  override validation). With `--live`, additionally (read-only): GET the Jira
  project (`/rest/api/3/project/{key}`), verify every `status_map` value exists
  in `/rest/api/3/status` (or the project statuses endpoint), verify each
  configured custom field id exists in `/rest/api/3/field`, GET the board when
  sprint features are enabled, and check GitHub token validity/scopes via a
  `viewer` GraphQL query + `X-OAuth-Scopes` header. Print one line per check;
  exit non-zero on any failure.

### upstream_jira_sync/digest.py  (from OLD/digest.py, 545 LOC — read in chunks, R16)
```python
EventKind = Literal["pr_linked", "story_points_set", "issue_claimed",
                    "manual_transition", "manual_points_change",
                    "ticket_created", "pr_orphaned"]

@dataclass(frozen=True) class DigestEvent      # same fields as old
@dataclass(frozen=True) class DigestReport     # same fields as old

def read_bot_events(state_path: str, window_start: datetime,
                    team_github: set[str] | None = None) -> list[DigestEvent]
def aggregate(...) / jira_changes_to_events(...) / events_to_json(...) / window_for(...)
def render_markdown(report, narrative, review_section) -> str
def post_digest_discussion(*, token: str, repo: str, category_slug: str,
                           title: str, body: str) -> str
def run_digest(*, config: AppConfig, state_path: str, days: int, post: bool,
               use_ai: bool, now: datetime | None = None) -> int
def main() -> int
```
Changes:
- `read_bot_events` reads ONLY the state file's `digest.events` namespace
  (R11) — no more scraping `github_user` out of dedup entries.
- Discussion title: `f"{config.digest_title_prefix} — {date} ({n} event(s))"`
  (the old hardcoded team prefix is config now, R3).
- Rendering: groups/rows sorted alphabetically by GitHub handle or grouped by
  repo — never by event/count volume (R12). Assignee attribution renders the
  GitHub handle, never the Jira email (R10).
- `_ai_narrative` builds the provider via `load_provider(config.llm)` (R9).
- Jira change search, manual-event filtering, dry-run/post flow faithful.

### upstream_jira_sync/emailer.py  (from OLD/emailer.py, 56 LOC)
Faithful port.
```python
class GmailNotifier(BaseHTTPClient):
    def __init__(self, from_addr: str = "", base_url: str = "") -> None
    def send(self, to: str, subject: str, body: str) -> None
```
Changes: log line becomes `"Sent low-confidence ping for @%s"` style — never
log the recipient email address (R10).

### upstream_jira_sync/review_activity.py  (from OLD/review_activity.py, 107 LOC)
```python
@dataclass(frozen=True)
class MemberReviewStats:
    github: str
    prs_reviewed: int
    review_comments: int
    approvals: int

def fetch_review_stats(github: GitHubClient, team: list[TeamMember],
                       window_start: datetime,
                       bot_logins: frozenset[str]) -> list[MemberReviewStats]
def render_review_section(stats: list[MemberReviewStats], intro: str) -> str
```
Changes: the hardcoded `_BOT_LOGINS` frozenset is gone; callers pass
`frozenset(config.bot_logins)` (R3). Table remains alphabetical by handle with
a team-total row; no volume ranking (R12).

### upstream_jira_sync/llm/vertex.py  (from sync/vertex.py, 67 LOC)
```python
class VertexProvider(BaseHTTPClient):
    def __init__(self, settings: LLMSettings) -> None
        # project=settings.vertex_project, region=settings.vertex_region,
        # model=settings.model, mock base_url=settings.base_url
    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str
```
Faithful port of `VertexAnthropicClient`: same URL construction
(`{region}-aiplatform.googleapis.com/.../publishers/anthropic/models/{model}:rawPredict`),
lazy `google.auth` import (google-auth ships in the `[vertex]` extra), ADC
token refresh, `anthropic_version: vertex-2023-10-16` payload. Changes: model
bound at construction; method renamed `create_message` -> `complete` with the
`model` parameter removed (R9).

### upstream_jira_sync/llm/anthropic.py  (new, R9)
```python
_API_URL = "https://api.anthropic.com/v1/messages"

class AnthropicProvider(BaseHTTPClient):
    def __init__(self, settings: LLMSettings) -> None
        # api key from env ANTHROPIC_API_KEY (raise ValueError if unset and no
        # settings.base_url mock); headers: x-api-key, anthropic-version: 2023-06-01
    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str
```
POST body `{model, max_tokens, system, messages:[{role:"user",content:user_message}]}`;
return `resp.json()["content"][0]["text"].strip()`. Honors `settings.base_url`
for `--mock-url` (routes to `{base_url}/v1/messages`). Uses `BaseHTTPClient`
retry/backoff.

---

## Deployment template (separate repo: /myworkspace/upstream-jira-sync-team-template)
Not part of this package. Contains: `config.yaml` (from config.example.yaml),
`.gitignore` with `team_roster.yaml` and `sync_state.json` (R10),
`team_roster.yaml.example`, and one GitHub Actions workflow that pins a
released `upstream-jira-sync` version, restores/saves `sync_state.json` via cache,
and injects secrets (`GITHUB_TOKEN/GH_PAT`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
`ROSTER_YAML` or a checked-out private roster, `ANTHROPIC_API_KEY` or GCP
workload identity).

## Porter checklist (every module)
1. Read the old file completely (chunked when >500 lines, R16).
2. Implement exactly the signatures above; faithful logic elsewhere.
3. Grep the finished file for tenant literals (R3) and `jira_email` in log
   calls (R10) before finishing.
4. Keep the module under ~500 LOC; no dead code, minimal comments (R14).

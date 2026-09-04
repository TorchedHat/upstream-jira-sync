# Changelog

All notable changes to upstream-jira-sync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (see CONTRIBUTING.md for what counts as
breaking).

## [Unreleased]

### Added

- `llm.models`: per-task model routing. Any of `match`, `estimate`, `summarize`,
  `dedupe`, `claim`, `team`, `rfc`, `digest` can name its own model; the rest
  fall back to `llm.model`. The CLI builds and preflights one provider per
  distinct model, so a cheap classifier can sit next to a stronger matcher
  without a second provider config. Recommended split (see
  `config.example.yaml`): `team`, `claim`, `rfc` and `summarize` on Haiku 4.5;
  `match`, `estimate`, `dedupe` and `digest` on the default model, since a
  wrong answer there writes to Jira or is read by people.
- `llm.thinking` (default `off`) and `llm.effort` (default `low`): control
  reasoning on models that think by default (Sonnet 5).
  `off` sends `thinking: disabled`, which is what the pre-Sonnet-5 setup
  effectively ran with and is plenty for these short classification calls;
  `adaptive` sends `thinking: adaptive` and pads `max_tokens` by 1024 so
  thinking cannot starve the answer. `effort` goes out as
  `output_config.effort` either way (`xhigh`/`max` need `adaptive`). Models
  that do not think by default (Opus 4.8, other 4.x, Haiku) get no thinking
  fields at all.

- Two zero-cost matcher steps run before the model. A PR whose title or body
  names a Jira key from the candidate pool (`PROJ-123`) is matched to that
  ticket directly at high confidence, logged as `Key matched PR #<n> -> <key>`.
  When more than `MATCH_SHORTLIST_SIZE` (10) tickets are open, the pool is
  trimmed to the 10 whose summary and description share the most words with
  the PR, each word weighted by how rare it is across the pool so filler and
  project-wide vocabulary carry no weight (ties keep the newest-updated
  first) before the prompt is built, logged as
  `Shortlisted 10 of <n> candidate tickets`. Both run inside `--dry-run`.
- The matcher consults `state.json` before calling the AI: a PR whose comment
  record already links it to a ticket is re-matched from state, and a
  low-confidence verdict is remembered under `low_conf_matches` so the same
  PR is not re-scored every night inside the 48h poll window.
- `automation_opt_out_labels`: Jira labels that exempt a card from bot-driven
  metadata writes. The sprint sweep and the team label / Team field backfill
  both skip a card carrying one. This gives human-owned cards that sit in an
  active status indefinitely — standing status-report issues, planning
  placeholders — a self-service way to stop being re-added to the current
  sprint after someone removes them by hand. Matched case-insensitively.

  Ships enabled, defaulting to `[no-automation]`, so labelling a card works
  without any config change. Set the key to rename the label or add more (it
  replaces the default rather than extending it), or to `[]` to disable.

### Changed

- PR link comments on Jira read as a short automated note instead of exposing
  matcher internals. The status line is now `Jira status set to <status>. PR
  last updated <date>.`, and the old `Match confidence: high — <reason>` line
  is replaced by `Linked to this ticket automatically. <reason>`; tickets the
  bot created say so in plain words, and the `state`/`auto` verdict tokens
  never appear in Jira. The URL-match reason now reads `The PR references
  <issue url>, which is already linked on this ticket.`
- A Messages API response that stops on `max_tokens` now raises `LLMError`
  (a retryable failed attempt) instead of being parsed as a truncated answer.

### Fixed

- Sonnet 5 requests used to omit `thinking`, so the model thought
  adaptively at effort `high` and the short `max_tokens` backstops (128-256)
  were spent on `thinking` blocks before any answer: nightly runs logged
  `returned no text content (blocks: ['thinking'], stop_reason: max_tokens)`
  for the matcher and team classifier, plus truncated match JSON. Thinking is
  now disabled by default on those models (see `llm.thinking`).

## [0.1.1] - 2026-08-06

### Fixed

- A stored `not_claiming` comment classification no longer permanently blocks
  ticket creation when a roster member later opens a PR that closing-references
  the same issue. The PR is the stronger claim signal and supersedes the old
  verdict. Every other classification still dedups, so issues are not
  reprocessed on later runs.

## [0.1.0] - 2026-07-23

### Added

- Initial release: GitHub-to-Jira sync for teams doing upstream contribution
  work — AI PR-ticket matching, config-driven status transitions
  (`status_map`, transitions resolved by target status id), story point
  estimation, ticket auto-creation from claimed issues, and optional sprint
  tagging/sweeping/provisioning, weekly digest, review activity, RFC container
  issues, manual override persistence, and team assignment.
- Pluggable LLM providers via the `upstream_jira_sync.llm` entry-point group;
  `vertex` (install extra `[vertex]`) and `anthropic` ship built in.
- Packaged default AI prompts with per-team overrides via `skills_dir` and
  template-variable validation.
- `check-config` doctor command (offline schema/roster/prompt validation;
  `--live` authenticated preflight).
- Composite GitHub Action (`action.yml`) wrapping `sync` and `check-config`.
- Privacy invariants: roster via `ROSTER_YAML` secret, no emails in logs or
  HTTP error messages, per-person attribution only under the state file's
  `digest` namespace, no activity-volume ranking, tenant-literal leak-grep in
  CI.
- `jira_components` setting: Jira components applied to every auto-created
  ticket (Stories, PR tickets, and container issues).
- `ignore_activity_authors` setting: activity by configured bot logins (plus
  any App-typed or `[bot]`-suffixed account) no longer counts as PR activity —
  it cannot pull a PR back into the sync window, reopen a stale-closed ticket,
  or reset the staleness clock. New `bot_activity` outcome counter.
- Co-author crediting on multi-author PRs: team-roster commit authors get an
  attributed Jira mention note on the PR's tracking ticket, an optional append
  to a `contributors_field` multi-user picker, and a `co_author_noted` digest
  event. New `--member` runs still recognize the full roster.
- `GitHubClient.get_prs_by_filter` public library API: fetch PRs by any
  combination of authors (OR), labels (AND), created date range, and
  open/draft/merged state, for external tools consuming this package as a
  library.

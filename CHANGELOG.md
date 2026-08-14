# Changelog

All notable changes to upstream-jira-sync are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (see CONTRIBUTING.md for what counts as
breaking).

## [Unreleased]

### Added

- `automation_opt_out_labels`: Jira labels that exempt a card from bot-driven
  metadata writes. The sprint sweep and the team label / Team field backfill
  both skip a card carrying one. This gives human-owned cards that sit in an
  active status indefinitely — standing status-report issues, planning
  placeholders — a self-service way to stop being re-added to the current
  sprint after someone removes them by hand. Defaults to empty (no change in
  behavior until configured); matched case-insensitively.

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

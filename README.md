# upstream-jira-sync

Config-driven GitHub-to-Jira sync framework for teams doing upstream
contribution work. A nightly bot mirrors your team's activity in upstream
GitHub repos (PRs, issues, reviews) into your Jira project so engineers never
hand-maintain tickets for work that already lives on GitHub.

## What it does

- **AI PR-ticket matching.** An LLM matches each team member's open and merged
  PRs to their open Jira tickets. No fuzzy fallback: if the model is not
  confident, the PR is skipped explicitly (optionally with an email ping).
- **Status transitions.** PR lifecycle (draft, in review, changes requested,
  merged, closed) maps to four canonical states (`todo`, `in_progress`,
  `review`, `done`), which your config maps to your project's actual status
  names. Transitions are resolved by target status, never by transition name,
  so any workflow shape works.
- **Story point estimation.** Optionally estimates points for matched tickets
  and writes them to your instance's points field.
- **Auto-create from claimed issues.** When a team member claims an upstream
  issue ("I'll take this"), the bot can create a tracking ticket, summarize the
  issue into the description, and link everything, with LLM dedup against
  existing tickets.
- **Sprints (optional).** Date-math sprint tagging, a nightly sweep of open
  tickets into the current sprint, and rolling pre-creation of upcoming
  sprints on your board.
- **Weekly digest (optional).** Posts a summary of bot and review activity to
  a GitHub Discussion. Rendering is alphabetical or grouped by repo, never
  ranked by volume.
- **RFC container issues (optional).** Upstream issues titled `[RFC]`/`RFC:`
  become container issues (e.g. Epics); PRs referencing them are parented
  underneath.
- **Manual override respect.** Human edits in Jira are detected via the
  changelog and preserved until the PR moves to a new stage.

Every optional feature ships disabled and starts in `shadow` mode when
enabled: it logs exactly what it would do without writing. You flip each
feature to `auto` only after its shadow output looks right.

## What runs when

With the deployment template's workflows:

- **Nightly sync** — for each roster member's recent upstream activity:
  matches PRs to tickets and moves them (opened/under review → your `review`
  status, significant rework → `in_progress`, merged → `done`), links the PR
  and leaves one audit comment, creates tickets where none exist, estimates
  points, closes tickets whose PRs went idle, parents RFC work under
  container issues, and leaves human status edits alone until the PR moves.
- **Weekly digest** — posts the week's bot and manual ticket activity as a
  GitHub Discussion.
- **On every PR to the deployment repo** — CI validates config, roster, and
  prompt overrides, so broken config can't merge.

Three rules cover most of the config: every feature is an `enable_*` flag
with a `shadow`/`auto` mode; `status_map` must match your Jira project's
exact status names (the only workflow coupling — verify with
`check-config --live`); and the roster alone decides whose activity is
synced.

## Splitting into teams

If your org has multiple sub-teams sharing one Jira project, the bot can tag
each ticket with its owning team so dashboards split cleanly:

```yaml
enable_team_assignment: true
team_assignment_mode: shadow   # flip to auto once tags look right
teams:
  - name: Runtime team          # exact display name
    label: runtime              # Jira label to apply
    # team_id: <Atlassian team id>  # only if dashboards use the native Team field
  - name: Tooling team
    label: tooling
# team_field: customfield_XXXXX     # required only when any team_id is set
```

An LLM classifies each PR against this list from its title, body, and file
paths. When classification needs per-team scope hints (paths, keywords), add
a `team_classification.md` override in your `skills_dir` — the packaged
prompt injects your team list via `{teams_section}`.

## The two-repo model

The framework is split from team data so it can be public and shared:

| Repo | Visibility | Contains |
|---|---|---|
| **upstream-jira-sync** (this repo) | public | All code, packaged default AI prompts, tests. Zero team-specific data; CI rejects tenant literals. |
| **your deployment repo** (from the [team template](https://github.com/TorchedHat/upstream-jira-sync/blob/main/config.example.yaml) | private | `config.yaml`, GitHub Actions workflows pinning a released framework version, secrets. No code. |

Fixes and features land here and every team picks them up by bumping one
pinned version tag. Team-specific values (Jira URL, project key, status names,
custom field ids, teams, prompts) live only in each deployment repo's config.

## Quickstart

Create a private repo from the deployment template and follow its onboarding
checklist. In short:

1. Fill in `config.yaml`: `jira_url`, `jira_project_key`, `status_map`,
   `github_repo`, `llm`.
2. Add secrets: a GitHub token, Jira service account credentials, the roster
   as the `ROSTER_YAML` secret, and one LLM provider's credentials.
3. Validate: `upstream-jira-sync check-config` (offline), then
   `upstream-jira-sync check-config --live` for an authenticated read-only
   preflight (Jira project reachable, `status_map` names exist in the
   workflow, custom fields exist, board reachable if sprints are enabled,
   GitHub token scopes).
4. First run in shadow: `upstream-jira-sync sync --dry-run --member some-handle`.
5. Enable features one at a time, shadow first, then `auto`.

## Run as a GitHub Action

This repo doubles as a composite GitHub Action; the `uses:` ref is the version
pin (the action pip-installs its own checkout, so ref and code cannot
disagree):

```yaml
- uses: YOUR_ORG/upstream-jira-sync@v0.1.0
  with:
    command: sync          # or check-config
    config: config.yaml    # default
    dry-run: "false"       # sync only
    # member: some-handle  # sync only: restrict to one roster entry
    # extras: vertex       # install extra for the Vertex AI provider
  env:
    GH_PAT: ${{ secrets.GH_PAT }}
    JIRA_EMAIL: ${{ secrets.JIRA_EMAIL }}
    JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}
    ROSTER_YAML: ${{ secrets.ROSTER_YAML }}
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

All inputs: `command`, `config`, `state-file`, `dry-run`, `member`,
`since-hours`, `live` (check-config preflight), `extras`, `python-version`.
Credentials always flow through `env`, never inputs. The deployment template's
workflows use this form; state caching stays in the calling workflow (see the
template's `sync.yml`). Pin exact tags until 1.0; from then on the floating
major tag (`@v1`) tracks compatible releases.

## CLI

```bash
upstream-jira-sync sync [--dry-run] [--member GITHUB_USERNAME] [--config PATH]
                   [--state-file PATH] [--since-hours N] [--mock-url URL]
upstream-jira-sync check-config [--config PATH] [--live]
```

- `--dry-run` logs every Jira write without making it.
- `--member` restricts the run to one roster entry (by GitHub handle).
- `--since-hours 336` overrides the poll window for a backfill.
- `--mock-url` routes GitHub, Jira, and LLM calls to a local mock server.

## Configuration reference

Copy `config.example.yaml` into your deployment repo as `config.yaml`. All
keys live under `settings:`. Secrets never go in this file (see
[Environment variables](#environment-variables)).

### Jira instance and workflow

| Key | Default | Description |
|---|---|---|
| `jira_url` | required | e.g. `https://yourcompany.atlassian.net` |
| `jira_project_key` | required | e.g. `PROJ` |
| `jira_bot_aliases` | `[]` | Previous service account emails/accountIds, so old changelog entries still count as bot writes for the override gate |
| `status_map` | required | Maps `todo` / `in_progress` / `review` / `done` to your project's exact status names |
| `issue_type` | `Story` | Issue type the bot creates and syncs |
| `container_issue_type` | `Epic` | Container type used for RFC tracking/parenting; excluded from PR-driven sync |
| `merge_labels` | `[]` | Labels a merge bot applies when it merges by close+label; empty means only natively merged PRs count |

### GitHub and roster

| Key | Default | Description |
|---|---|---|
| `github_repo` | required | List of `org/repo` upstream repos to watch |
| `roster_file` | `team_roster.yaml` | Path relative to the config file; ignored when `ROSTER_YAML` is set. Entries are `{github: <handle>, jira_email: <assignee email>}` |
| `bot_logins` | dependabot, github-actions | Bot accounts ignored in review-activity stats |

### LLM

| Key | Default | Description |
|---|---|---|
| `llm.provider` | `anthropic` | `anthropic`, `vertex`, or any installed entry point |
| `llm.model` | required | Model name passed to the provider |
| `llm.models` | `{}` | Per-task model overrides, keyed by task: `match`, `estimate`, `summarize`, `dedupe`, `claim`, `team`, `rfc`, `digest`. Unlisted tasks use `llm.model`. One provider is built per distinct model. Recommended: `team`, `claim`, `rfc`, `summarize` on Haiku 4.5; the rest on the default model (see `config.example.yaml`). |
| `llm.thinking` | `off` | `off` sends `thinking: disabled` to models that think by default (Sonnet 5); `adaptive` lets them think and pads `max_tokens` by 1024 so thinking cannot starve the answer. Opus 4.8, other 4.x models and Haiku get no thinking fields either way. |
| `llm.effort` | `low` | Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`), sent as `output_config.effort` to models that think by default. `xhigh`/`max` require `llm.thinking: adaptive`. Set to `""` to omit `effort` and let the model choose. |
| `llm.vertex_project` | unset | Vertex only; or set `GOOGLE_CLOUD_PROJECT` |
| `llm.vertex_region` | `us-east5` | Vertex only |

### Tuning

| Key | Default | Description |
|---|---|---|
| `poll_interval_hours` | `48` | Activity window per run; override per run with `--since-hours` |
| `significant_comments_threshold` | `3` | CHANGES_REQUESTED review count that flips a ticket review -> in_progress |
| `ignore_pr_labels` | `[Stale]` | Open PRs carrying any of these labels are skipped |
| `automation_opt_out_labels` | `[no-automation]` | Jira labels that exempt a card from bot metadata writes (sprint sweep, team backfill); case-insensitive. Setting it replaces the default; `[]` disables |

### Story point estimation

| Key | Default | Description |
|---|---|---|
| `enable_estimation` | `false` | |
| `story_points_field` | unset | Your instance's points field id (`customfield_XXXXX`); required when estimation is enabled |

### Issue claiming / auto-create

| Key | Default | Description |
|---|---|---|
| `enable_auto_create` | `false` | Create tickets from claimed upstream issues |
| `claim_mode` | `shadow` | `shadow` \| `auto` |
| `stale_pr_close_days` | `21` | Close a ticket whose linked PRs idled this many days; `0` disables |
| `stale_pr_close_mode` | `shadow` | `shadow` \| `auto` |

### Sprints

| Key | Default | Description |
|---|---|---|
| `enable_sprint_tagging` | `false` | Tag matched tickets with the current sprint |
| `sprint_mode` | `shadow` | `shadow` \| `auto` |
| `sprint_anchor_date` / `sprint_anchor_number` | unset | A known sprint's start date and number; date math derives the current sprint from these |
| `sprint_length_days` | `14` | |
| `sprint_board_id` | unset | Board the bot reads/creates sprints on |
| `sprint_name_format` | `Sprint {number}` | Exact-name match on the board |
| `sprint_field` | unset | Sprint field id; required when any sprint feature is enabled |
| `enable_sprint_sweep` | `false` | Nightly add of open tickets to the current sprint |
| `sprint_sweep_mode` | `shadow` | `shadow` \| `auto` |
| `sprint_sweep_lookback_sprints` | `1` | |
| `enable_sprint_provision` | `false` | Rolling pre-creation of upcoming sprints |
| `sprint_provision_mode` | `shadow` | `shadow` \| `auto` |
| `sprint_provision_lookahead` | `2` | Pre-create current+1 .. current+lookahead |

**Exempting a card from the sweep.** A human-owned card that stays in an active
status — a standing status-report issue — is re-added every night even after
someone removes it by hand. Label it `no-automation` and the sweep and the team
backfill both skip it. Existing sprint values are left alone.

### Weekly digest

| Key | Default | Description |
|---|---|---|
| `digest_enabled` | `false` | Also gates whether per-person digest events are recorded in the state file |
| `digest_include_manual` | `true` | Include manual Jira changes detected via changelog |
| `digest_include_reviews` | `true` | Include review-activity stats |
| `digest_repo` | unset | `org/repo` whose Discussions receive the digest |
| `digest_category_slug` | `announcements` | |
| `digest_title_prefix` | `Weekly digest` | |

### Manual override persistence

| Key | Default | Description |
|---|---|---|
| `enable_manual_override` | `true` | Respect human edits in Jira until the PR moves stage |
| `manual_override_mode` | `shadow` | `shadow` logs skips but still writes; `auto` skips |
| `manual_override_fields` | `[status]` | Fields the gate protects |

### Team assignment

| Key | Default | Description |
|---|---|---|
| `enable_team_assignment` | `false` | Classify each PR into one of your teams |
| `team_assignment_mode` | `shadow` | `shadow` \| `auto` |
| `teams` | `[]` | List of `{name, label, team_id (optional Atlassian team UUID)}`; the classification prompt is templated from this list at runtime |
| `team_field` | unset | Team custom field id; required when any team sets `team_id` |

### RFC / container-issue tracking

| Key | Default | Description |
|---|---|---|
| `enable_rfc_epics` | `false` | Issues titled `[RFC]`/`RFC:` become container issues |
| `rfc_epic_mode` | `shadow` | `shadow` \| `auto` |
| `rfc_overrides` | `{}` | Issue URL -> existing container issue key |
| `epic_name_field` | unset | Only for instances requiring an "Epic Name" field at create time; omitted from the create payload when unset |

### Low-confidence email pings

| Key | Default | Description |
|---|---|---|
| `enable_low_conf_email` | `false` | Email the PR author when matching confidence is low |
| `low_conf_email_mode` | `shadow` | `shadow` \| `auto` |
| `low_conf_email_from` | unset | From address |

### AI prompt overrides

| Key | Default | Description |
|---|---|---|
| `skills_dir` | unset | Directory of per-file overrides for the packaged `skills/*.md` prompts (relative paths resolve against the config file and the directory must exist); any file not present falls back to the packaged default. Overrides must keep the template variables the framework injects; `check-config` fails fast listing any missing ones |

## Environment variables

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` (or `GH_PAT`) | GitHub token with `repo` + `read:discussion` scopes |
| `JIRA_EMAIL` | Jira service account email |
| `JIRA_API_TOKEN` | Jira API token |
| `JIRA_ACCOUNT_ID` | Optional; the service account's Atlassian accountId (sharpens override detection) |
| `ROSTER_YAML` | Optional; the roster as a YAML string, overriding `roster_file` |
| `ANTHROPIC_API_KEY` | Required by `llm.provider: anthropic` |
| `GOOGLE_APPLICATION_CREDENTIALS` | `llm.provider: vertex`; path to GCP credentials JSON |
| `GOOGLE_CLOUD_PROJECT` | `llm.provider: vertex`; or set `llm.vertex_project` |

## LLM providers

Two providers ship with the package; both expose the same `complete()` surface
to the AI classes.

**anthropic** (default) calls the Anthropic Messages API directly.

```yaml
llm:
  provider: anthropic
  model: claude-sonnet-5
  thinking: off                # these are short classification calls
  effort: low
  models:                      # recommended split; the rest stay on llm.model
    team: claude-haiku-4-5
    claim: claude-haiku-4-5
    rfc: claude-haiku-4-5
    summarize: claude-haiku-4-5
```

Set `ANTHROPIC_API_KEY`. No install extra needed.

**vertex** calls the same models through GCP Vertex AI with Application
Default Credentials.

```yaml
llm:
  provider: vertex
  model: your-vertex-model-id
  vertex_project: your-gcp-project-id
  vertex_region: us-east5
```

Install with the extra: `pip install "upstream-jira-sync[vertex]"`. Authenticate
via `GOOGLE_APPLICATION_CREDENTIALS` or ambient ADC (e.g. workload identity).

**Third-party providers** plug in through the `upstream_jira_sync.llm` entry-point
group. A package exposing a class with
`__init__(settings: LLMSettings)` and
`complete(system, user_message, max_tokens) -> str` under that group becomes
selectable as `llm.provider: <entry-point-name>`.

## Privacy

- The roster maps GitHub handles to Jira assignee emails and should never be
  committed: supply it via the `ROSTER_YAML` env var, or keep `roster_file`
  gitignored (the deployment template does this by default).
- Logs reference GitHub handles only; Jira emails are never logged.
- Per-person attribution in the state file exists only under the `digest`
  namespace and only when the digest is enabled; core dedup keys carry no
  user attribution.
- No user-facing output ranks team members by activity volume.

## Development

```bash
git clone <this repo> && cd upstream-jira-sync
pip install -e ".[dev]"          # add ,vertex to work on the vertex provider

python -m pytest tests/ -v       # all tests
ruff check . && ruff format --check .
```

Integration testing without real APIs uses the bundled mock server, which
simulates GitHub GraphQL/REST, Jira REST v3, and both LLM provider APIs:

```bash
python tests/mock_server.py      # starts on port 9999
upstream-jira-sync sync --config tests/fixtures/config.test.yaml \
    --mock-url http://localhost:9999 --dry-run
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR, in particular the
tenant-literal rule: this repo must stay free of any team-specific names,
hosts, field ids, or emails, and CI greps for leaks on every push.

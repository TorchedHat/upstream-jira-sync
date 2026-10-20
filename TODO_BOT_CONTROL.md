# Bot Control Feature Implementation TODO

## PR #5 (Current - Review Fixes)
- [ ] **Verify CI passes on GitHub Actions** - check PR #5 status
- [ ] **Merge PR #5** once CI is green

---

## PR #6 (New - Bot Control Feature Implementation)

### Phase 1: Core Control Implementation ✅
- [x] Create `upstream_jira_sync/control.py` with `BotControl` class
  - [x] Implement `get_control_state()` method
  - [x] Add validation for enabled flag (only "enabled", "disabled", "suspended")
  - [x] Add validation for sync_interval_hours (must be > 0)
  - [x] Implement fail-loud behavior (no fallback)
  - [x] Log all queries at INFO level
- [x] Write unit tests for control.py (15 test cases)
  - [x] test_bot_control_disabled_in_config()
  - [x] test_bot_control_enabled_flag()
  - [x] test_bot_control_disabled_flag()
  - [x] test_bot_control_suspended_flag()
  - [x] test_bot_control_interval_override()
  - [x] test_bot_control_interval_as_string()
  - [x] test_bot_control_issue_missing_aborts()
  - [x] test_bot_control_query_fails_aborts()
  - [x] test_bot_control_missing_flag_field_aborts()
  - [x] test_bot_control_invalid_flag_value_aborts()
  - [x] test_bot_control_invalid_interval_aborts()
  - [x] test_bot_control_interval_zero_aborts()
  - [x] test_bot_control_interval_negative_aborts()
  - [x] test_bot_control_no_config_issue_aborts()
  - [x] test_bot_control_interval_optional()

### Phase 2: Integration into CLI ✅
- [x] Modify `upstream_jira_sync/cli.py:run_sync()`
  - [x] Import BotControl class
  - [x] Add check after config load, before orchestrator.run()
  - [x] Handle enabled=false case (clean exit)
  - [x] Handle enabled=true with missing issue (abort with error)
  - [x] Override config.poll_interval_hours if set in Jira
  - [x] Log control state at INFO level

### Phase 3: Configuration Extension ✅
- [x] Update `upstream_jira_sync/config.py` (AppConfig dataclass)
  - [x] Add `bot_control_enabled: bool = False`
  - [x] Add `bot_control_issue: str = ""`
  - [x] Add `bot_control_flag_field: str = ""`
  - [x] Add `bot_control_interval_field: str = ""`
- [x] Update `config.example.yaml`
  - [x] Add all 4 new fields with defaults and comments
- [x] Verify config loads without errors

### Phase 4: Testing ✅
- [x] Run unit tests: `pytest tests/test_control.py`
- [x] Verify all 15 tests pass
- [x] Check module compilation

### Phase 5: Local Live Testing (Against Real PyTorch Repo) ⏳ NEXT
- [ ] Clone pytorch/pytorch repo (or use existing)
- [ ] Create test Jira issue with control fields
- [ ] Configure bot_control_enabled=true in local config
- [ ] Test Scenario 1: Bot enabled (normal flow)
  - [ ] Run `upstream-jira-sync sync --dry-run`
  - [ ] Verify control issue queried
  - [ ] Verify sync runs normally
- [ ] Test Scenario 2: Bot disabled
  - [ ] Set control issue flag='disabled'
  - [ ] Run `upstream-jira-sync sync --dry-run`
  - [ ] Verify clean exit with log message
- [ ] Test Scenario 3: Interval override
  - [ ] Set control issue sync_interval_hours=24
  - [ ] Run `upstream-jira-sync sync --dry-run`
  - [ ] Verify interval override in logs
- [ ] Test Scenario 4: Missing control issue
  - [ ] Delete control issue from Jira
  - [ ] Run `upstream-jira-sync sync --dry-run`
  - [ ] Verify abort with clear error message
- [ ] Test Scenario 5: Invalid field value
  - [ ] Set control issue flag to invalid value
  - [ ] Run `upstream-jira-sync sync --dry-run`
  - [ ] Verify abort with validation error
- [ ] Document test results

### Phase 6: Documentation & PR
- [ ] Update `action.md` or relevant docs with bot control feature
- [ ] Write clear commit message with fail-loud rationale
- [ ] Create PR with:
  - [ ] Title: "feat: Add Jira-based bot control (enable/disable, frequency override)"
  - [ ] Description: rationale, config example, fail-loud behavior
  - [ ] Reference CONTROL_FEATURE_DESIGN.md
- [ ] Ensure CI passes (pytest, linting)
- [ ] Get review approval
- [ ] Merge PR #6

---

## Verification Checklist
- [ ] No graceful fallback anywhere (all errors abort)
- [ ] All control queries logged at INFO level (visible in logs)
- [ ] Validation strict (aborts on invalid values)
- [ ] Local live testing passed against PyTorch repo
- [ ] Unit tests comprehensive and passing
- [ ] Config backward-compatible (all fields optional, defaults disable feature)
- [ ] No new secrets or credentials needed
- [ ] Code follows existing patterns (JiraClient, AppConfig, etc.)

---

## Notes
- Live testing is local-only (before pushing PR), not in CI
- Fail-loud principle: no fallback, no defaults, clear errors
- PR #5 review fixes are separate and ready to merge
- Design document: CONTROL_FEATURE_DESIGN.md (complete, ready to follow)

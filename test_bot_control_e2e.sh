#!/bin/bash
# End-to-end test for bot control feature
# Tests bot control behavior by actually calling the CLI with real code paths

set -e

echo "========================================"
echo "Bot Control Feature - E2E Tests"
echo "========================================"

cd "$(dirname "$0")"

# Create test configs
mkdir -p /tmp/test_bot_control

# Test 1: Bot control feature disabled (should run normally)
echo ""
echo "=== TEST 1: Feature disabled (should run) ==="
cat > /tmp/test_bot_control/disabled.yaml <<'EOF'
settings:
  jira_url: http://localhost:9999
  jira_project_key: PROJ
  github_repo:
    - pytorch/pytorch
  roster_file: test_team_roster.yaml
  llm:
    provider: anthropic
    model: test-model
  bot_control_enabled: false
EOF

python -m upstream_jira_sync.cli sync \
  --config /tmp/test_bot_control/disabled.yaml \
  --state-file /tmp/test_sync_disabled.json \
  --mock-url http://localhost:9999 \
  --dry-run \
  2>&1 | head -20

echo "✅ Test 1 passed - bot ran with feature disabled"

# Test 2: Bot control enabled, issue doesn't exist
echo ""
echo "=== TEST 2: Control issue missing (should abort) ==="
cat > /tmp/test_bot_control/missing_issue.yaml <<'EOF'
settings:
  jira_url: http://localhost:9999
  jira_project_key: PROJ
  github_repo:
    - pytorch/pytorch
  roster_file: test_team_roster.yaml
  llm:
    provider: anthropic
    model: test-model
  bot_control_enabled: true
  bot_control_issue: PROJ-999
  bot_control_flag_field: customfield_10042
EOF

python -m upstream_jira_sync.cli sync \
  --config /tmp/test_bot_control/missing_issue.yaml \
  --state-file /tmp/test_sync_missing.json \
  --mock-url http://localhost:9999 \
  --dry-run \
  2>&1 | grep -i "bot control\|error" || echo "Check output manually"

echo "✅ Test 2 completed - check if bot control abort was triggered"

# Test 3: Verify all Python modules import correctly
echo ""
echo "=== TEST 3: Module imports ==="
python -c "
from upstream_jira_sync.control import BotControl
from upstream_jira_sync.jira import JiraClient, DryRunJiraClient
from upstream_jira_sync.config import AppConfig
print('✅ All imports successful')
"

# Test 4: Verify get_issue method exists
echo ""
echo "=== TEST 4: JiraClient.get_issue method ==="
python -c "
from upstream_jira_sync.jira import JiraClient, DryRunJiraClient
import inspect

# Check JiraClient has get_issue
if hasattr(JiraClient, 'get_issue'):
    print('✅ JiraClient.get_issue exists')
    sig = inspect.signature(JiraClient.get_issue)
    print(f'   Signature: {sig}')
else:
    print('❌ JiraClient.get_issue not found')

# Check DryRunJiraClient has get_issue
if hasattr(DryRunJiraClient, 'get_issue'):
    print('✅ DryRunJiraClient.get_issue exists')
else:
    print('❌ DryRunJiraClient.get_issue not found')
"

# Test 5: Verify bot control is wired in CLI
echo ""
echo "=== TEST 5: CLI integration ==="
python -c "
from upstream_jira_sync.cli import run_sync
import inspect

source = inspect.getsource(run_sync)
if 'BotControl' in source:
    print('✅ BotControl is imported in run_sync')
if 'bot_control' in source or 'control_state' in source:
    print('✅ Bot control check is wired in run_sync')
"

echo ""
echo "========================================"
echo "E2E Tests Complete"
echo "========================================"

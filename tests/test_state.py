"""SyncState persistence, dedup, debounce, TTL pruning, and the digest namespace."""

import json
import os
from datetime import datetime, timedelta, timezone

from upstream_jira_sync.state import SCHEMA_VERSION, SyncState
from upstream_jira_sync.models import STATE_TTL_DAYS

PR = "https://github.com/exampleorg/widgets/pull/1"


class TestSyncStateBasics:
    def test_record_and_check_comment_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = SyncState(path=path)
        state.record_comment(PR, "PROJ-100", "review")
        assert state.is_commented(PR, "PROJ-100") is True
        assert SyncState(path=path).is_commented(PR, "PROJ-100") is True

    def test_read_only_mode_does_not_write(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = SyncState(path=path, read_only=True)
        state.record_comment(PR, "PROJ-100", "review")
        assert not os.path.exists(path)

    def test_load_corrupt_file_returns_empty_state(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not valid json {{{")
        assert SyncState(path=str(path)).is_commented(PR, "PROJ-100") is False

    def test_foreign_schema_version_ignored(self, tmp_path):
        # Files from other tools/versions are not migrated; state starts fresh.
        path = str(tmp_path / "state.json")
        recent = datetime.now(timezone.utc).isoformat()
        blob = {
            "version": SCHEMA_VERSION + 2,
            "comments": {
                f"{PR}::PROJ-1": {"last_status": "review", "commented_at": recent}
            },
        }
        with open(path, "w") as f:
            json.dump(blob, f)
        assert SyncState(path=path).is_commented(PR, "PROJ-1") is False

    def test_writes_current_schema_version(self, tmp_path):
        path = str(tmp_path / "state.json")
        SyncState(path=path).record_comment(PR, "PROJ-1", "review")
        with open(path) as f:
            on_disk = json.load(f)
        assert on_disk["version"] == SCHEMA_VERSION
        assert "comments" in on_disk and "digest" in on_disk

    def test_stale_entries_pruned_on_load(self, tmp_path):
        path = str(tmp_path / "state.json")
        blob = {
            "version": SCHEMA_VERSION,
            "comments": {
                f"{PR}::PROJ-1": {
                    "last_status": "done",
                    "commented_at": "2020-01-01T00:00:00+00:00",
                },
                f"{PR}::PROJ-2": {
                    "last_status": "review",
                    "commented_at": "2099-01-01T00:00:00+00:00",
                },
            },
        }
        with open(path, "w") as f:
            json.dump(blob, f)

        state = SyncState(path=path)
        assert state.is_commented(PR, "PROJ-1") is False
        assert state.is_commented(PR, "PROJ-2") is True


class TestEstimationAndIssueTracking:
    def test_estimation_roundtrip_and_read_only(self, tmp_path):
        path = str(tmp_path / "s.json")
        state = SyncState(path=path)
        assert state.is_estimated(PR, "PROJ-1") is False
        state.record_estimation(PR, "PROJ-1", 5)
        assert state.is_estimated(PR, "PROJ-1") is True
        assert SyncState(path=path).is_estimated(PR, "PROJ-1") is True

        ro = SyncState(path=str(tmp_path / "ro.json"), read_only=True)
        ro.record_estimation(PR, "PROJ-1", 5)
        assert ro.is_estimated(PR, "PROJ-1") is False

    def test_issue_classification_roundtrip(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        url = "https://github.com/exampleorg/widgets/issues/1"
        assert state.is_issue_processed(url) is False
        state.record_issue_classification(url, "claiming", "test")
        assert state.is_issue_processed(url) is True
        state.set_issue_ticket(url, "PROJ-9")
        assert state._data["classifications"][url]["ticket_key"] == "PROJ-9"

    def test_rfc_epic_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        rfc_url = "https://github.com/exampleorg/widgets/issues/12345"
        state = SyncState(path=path)
        assert state.get_rfc_epic(rfc_url) is None
        state.record_rfc_epic(rfc_url, "PROJ-9")
        assert state.get_rfc_epic(rfc_url) == "PROJ-9"
        assert SyncState(path=path).get_rfc_epic(rfc_url) == "PROJ-9"


class TestPRTracking:
    def test_record_and_check_pr_tracked_persists(self, tmp_path):
        path = str(tmp_path / "s.json")
        SyncState(path=path).record_pr_tracked(PR, "PROJ-9")
        reloaded = SyncState(path=path)
        assert reloaded.is_pr_tracked(PR) is True
        assert reloaded.get_tracked_ticket_key(PR) == "PROJ-9"

    def test_record_pr_orphaned_is_idempotent(self, tmp_path):
        state = SyncState(path=str(tmp_path / "state.json"))
        state.record_pr_tracked(PR, "PROJ-100")
        assert state.record_pr_orphaned(PR) is True
        assert state.record_pr_orphaned(PR) is False
        assert state.record_pr_orphaned("https://example.com/pr/never-tracked") is False

    def test_cancel_debounce_persists_across_reload(self, tmp_path):
        path = str(tmp_path / "s.json")
        assert SyncState(path=path).record_pr_cancel_seen(PR, "PROJ-9") is False
        assert SyncState(path=path).record_pr_cancel_seen(PR, "PROJ-9") is True

    def test_clear_cancel_resets_debounce(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        state.record_pr_cancel_seen(PR, "PROJ-9")
        state.clear_pr_cancel_seen(PR, "PROJ-9")
        assert state.record_pr_cancel_seen(PR, "PROJ-9") is False

    def test_cancel_comment_dedup_is_separate_from_link_comment(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        state.record_comment(PR, "PROJ-9", "review")
        assert state.record_cancel_commented(PR, "PROJ-9") is True
        assert state.record_cancel_commented(PR, "PROJ-9") is False


class TestSnapshots:
    def test_snapshot_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = SyncState(path=path)
        state.record_pr_state_snapshot("PROJ-7", "merged", f"{PR}", "done")

        snap = SyncState(path=path).get_pr_state_snapshot("PROJ-7")
        assert snap is not None
        assert snap["pr_state"] == "merged"
        assert snap["pr_url"] == PR
        assert snap["resolved_status"] == "done"
        assert "observed_at" in snap


class TestDigestNamespace:
    """Per-person attribution lives ONLY under the digest namespace (R11)."""

    def test_core_dedup_entries_carry_no_user_attribution(self, tmp_path):
        path = str(tmp_path / "s.json")
        state = SyncState(path=path)
        state.record_comment(
            PR, "PROJ-1", "review", match_confidence="high", match_reason="r"
        )
        state.record_estimation(PR, "PROJ-1", 5)
        state.record_pr_tracked(PR, "PROJ-1")
        state.record_issue_classification(
            "https://github.com/exampleorg/widgets/issues/1", "claiming", "r"
        )

        with open(path) as f:
            on_disk = json.load(f)
        assert "github_user" not in json.dumps(on_disk)
        for ns in ("comments", "estimations", "tracking", "classifications"):
            for entry in on_disk[ns].values():
                assert "github_user" not in entry

    def test_digest_event_roundtrip(self, tmp_path):
        path = str(tmp_path / "s.json")
        state = SyncState(path=path)
        state.record_digest_event(
            "pr_linked",
            ticket_key="PROJ-1",
            pr_url=PR,
            github_user="octocat",
            new_value="review",
        )

        events = SyncState(path=path).read_digest_events("1970-01-01T00:00:00+00:00")
        assert len(events) == 1
        assert events[0]["kind"] == "pr_linked"
        assert events[0]["github_user"] == "octocat"
        assert events[0]["ticket_key"] == "PROJ-1"

    def test_read_digest_events_filters_by_window(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        state.record_digest_event("pr_linked", ticket_key="PROJ-1")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        assert state.read_digest_events(future) == []

    def test_stale_digest_events_pruned_on_load(self, tmp_path):
        path = str(tmp_path / "s.json")
        blob = {
            "version": SCHEMA_VERSION,
            "digest": {
                "events": [
                    {"observed_at": "2020-01-01T00:00:00+00:00", "kind": "pr_linked"},
                    {"observed_at": "2099-01-01T00:00:00+00:00", "kind": "pr_linked"},
                ]
            },
        }
        with open(path, "w") as f:
            json.dump(blob, f)

        events = SyncState(path=path).read_digest_events("1970-01-01T00:00:00+00:00")
        assert len(events) == 1
        assert events[0]["observed_at"].startswith("2099")

    def test_digest_read_only_noop(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"), read_only=True)
        state.record_digest_event("pr_linked", ticket_key="PROJ-1")
        assert state.read_digest_events("1970-01-01T00:00:00+00:00") == []


class TestMatcherMemory:
    """State the orchestrator consults before paying for the AI matcher."""

    def test_linked_ticket_prefers_most_recent_comment(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        assert state.get_linked_ticket_key(PR) == ""
        state.record_comment(PR, "PROJ-1", "review")
        state._data["comments"][f"{PR}::PROJ-1"]["commented_at"] = (
            "2026-01-01T00:00:00+00:00"
        )
        state.record_comment(PR, "PROJ-2", "review")
        assert state.get_linked_ticket_key(PR) == "PROJ-2"

    def test_linked_ticket_falls_back_to_tracked_ticket(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        state.record_pr_tracked(PR, "PROJ-9")
        assert state.get_linked_ticket_key(PR) == "PROJ-9"

    def test_linked_ticket_does_not_match_pr_url_prefix(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        state.record_comment(f"{PR}0", "PROJ-10", "review")
        assert state.get_linked_ticket_key(PR) == ""

    def test_low_conf_match_keyed_to_activity_timestamp(self, tmp_path):
        path = str(tmp_path / "s.json")
        state = SyncState(path=path)
        assert state.is_low_conf_unchanged(PR, "2026-09-01T00:00:00Z") is False
        state.record_low_conf_match(PR, "2026-09-01T00:00:00Z")
        reloaded = SyncState(path=path)
        assert reloaded.is_low_conf_unchanged(PR, "2026-09-01T00:00:00Z") is True
        assert reloaded.is_low_conf_unchanged(PR, "2026-09-02T00:00:00Z") is False

    def test_clear_low_conf_match(self, tmp_path):
        state = SyncState(path=str(tmp_path / "s.json"))
        state.record_low_conf_match(PR, "2026-09-01T00:00:00Z")
        state.clear_low_conf_match(PR)
        state.clear_low_conf_match(PR)  # idempotent
        assert state.is_low_conf_unchanged(PR, "2026-09-01T00:00:00Z") is False

    def test_low_conf_match_read_only_is_noop(self, tmp_path):
        path = str(tmp_path / "s.json")
        state = SyncState(path=path, read_only=True)
        state.record_low_conf_match(PR, "2026-09-01T00:00:00Z")
        assert state.is_low_conf_unchanged(PR, "2026-09-01T00:00:00Z") is False
        assert not os.path.exists(path)

    def test_low_conf_match_pruned_by_ttl(self, tmp_path):
        path = tmp_path / "s.json"
        stale = (
            datetime.now(timezone.utc) - timedelta(days=STATE_TTL_DAYS + 1)
        ).isoformat()
        blob = {
            "version": SCHEMA_VERSION,
            "low_conf_matches": {
                PR: {"activity_at": "2026-09-01T00:00:00Z", "matched_at": stale}
            },
        }
        path.write_text(json.dumps(blob))
        state = SyncState(path=str(path))
        assert state.is_low_conf_unchanged(PR, "2026-09-01T00:00:00Z") is False

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from upstream_jira_sync.models import STATE_TTL_DAYS

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_NAMESPACES: tuple[str, ...] = (
    "comments",
    "estimations",
    "classifications",
    "tracking",
    "locks",
    "rfc_tracking",
    "low_conf_pings",
    "pr_state_snapshots",
    "co_contributions",
    "digest",
)


def _empty_state() -> dict[str, Any]:
    state: dict[str, Any] = {"version": SCHEMA_VERSION}
    for ns in _NAMESPACES:
        state[ns] = {}
    state["digest"] = {"events": []}
    return state


class SyncState:
    """JSON file tracking which PR/ticket combos have already been commented on."""

    _TIMESTAMP_FIELDS: ClassVar[dict[str, str]] = {
        "comments": "commented_at",
        "estimations": "estimated_at",
        "classifications": "classified_at",
        "tracking": "tracked_at",
        "locks": "locked_at",
        "rfc_tracking": "tracked_at",
        "low_conf_pings": "pinged_at",
        "pr_state_snapshots": "observed_at",
        "co_contributions": "noted_at",
    }

    def __init__(self, path: str = "sync_state.json", read_only: bool = False) -> None:
        self._path = path
        self._read_only = read_only
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """Load state from disk, prune stale entries, return empty state if missing."""
        try:
            with open(self._path) as f:
                raw = json.load(f)
            if isinstance(raw, dict) and raw.get("version") == SCHEMA_VERSION:
                data = _empty_state()
                for ns in _NAMESPACES:
                    bucket = raw.get(ns)
                    if isinstance(bucket, dict):
                        data[ns] = bucket
                if not isinstance(data["digest"].get("events"), list):
                    data["digest"] = {"events": []}
                self._prune_stale(data)
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return _empty_state()

    @classmethod
    def _prune_stale(cls, data: dict[str, Any]) -> None:
        """Remove entries older than STATE_TTL_DAYS across every namespace."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=STATE_TTL_DAYS)
        ).isoformat()

        total_pruned = 0
        for ns, ts_field in cls._TIMESTAMP_FIELDS.items():
            bucket = data.get(ns)
            if not isinstance(bucket, dict):
                continue
            stale = []
            for k, v in bucket.items():
                ts = v.get(ts_field, "")
                if ns == "locks":
                    # Cancel-debounce entries carry their own timestamps.
                    ts = max(
                        ts,
                        v.get("cancel_seen_at", ""),
                        v.get("cancel_commented_at", ""),
                    )
                if ts < cutoff:
                    stale.append(k)
            for k in stale:
                del bucket[k]
            total_pruned += len(stale)

        events = data["digest"].get("events", [])
        kept = [e for e in events if e.get("observed_at", "") >= cutoff]
        total_pruned += len(events) - len(kept)
        data["digest"]["events"] = kept

        if total_pruned:
            log.info(
                "  Pruned %d stale state entries (>%dd old).",
                total_pruned,
                STATE_TTL_DAYS,
            )

    def _save(self) -> None:
        """Atomic write: write to .tmp then os.replace()."""
        if self._read_only:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self._path)

    @staticmethod
    def _key(pr_url: str, ticket_key: str) -> str:
        return f"{pr_url}::{ticket_key}"

    def is_commented(self, pr_url: str, ticket_key: str) -> bool:
        """Check if we've already posted a comment for this PR on this ticket."""
        return self._key(pr_url, ticket_key) in self._data["comments"]

    def record_comment(
        self,
        pr_url: str,
        ticket_key: str,
        status: str,
        match_confidence: str = "",
        match_reason: str = "",
    ) -> None:
        """Record that a comment was posted. No-op in read-only mode."""
        if self._read_only:
            return
        entry: dict[str, Any] = {
            "last_status": status,
            "commented_at": datetime.now(timezone.utc).isoformat(),
        }
        if match_confidence:
            entry["match_confidence"] = match_confidence
            entry["match_reason"] = match_reason
        self._data["comments"][self._key(pr_url, ticket_key)] = entry
        self._save()

    def is_estimated(self, pr_url: str, ticket_key: str) -> bool:
        """Check if story points have already been set for this PR/ticket."""
        key = self._key(pr_url, ticket_key)
        entry = self._data["estimations"].get(key, {})
        return "story_points" in entry

    def record_estimation(self, pr_url: str, ticket_key: str, points: int) -> None:
        """Record that story points were set. No-op in read-only mode."""
        if self._read_only:
            return
        self._data["estimations"][self._key(pr_url, ticket_key)] = {
            "story_points": points,
            "estimated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def is_pr_tracked(self, pr_url: str) -> bool:
        """Check if a ticket has already been auto-created for this PR."""
        return pr_url in self._data["tracking"]

    def get_tracked_ticket_key(self, pr_url: str) -> str:
        """Return the ticket key originally recorded for this tracked PR, or empty."""
        entry = self._data["tracking"].get(pr_url, {})
        return entry.get("ticket_key", "")

    def record_pr_orphaned(self, pr_url: str) -> bool:
        """Mark a previously-tracked PR as orphaned. Returns True on first-time mark.

        Idempotent: subsequent calls return False so callers can log only once.
        """
        if self._read_only:
            return False
        entry = self._data["tracking"].get(pr_url)
        if entry is None or entry.get("orphaned_at"):
            return False
        entry["orphaned_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    def record_pr_tracked(self, pr_url: str, ticket_key: str) -> None:
        """Record that a tracking ticket was created for a PR. No-op in read-only mode."""
        if self._read_only:
            return
        self._data["tracking"][pr_url] = {
            "ticket_key": ticket_key,
            "tracked_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def get_rfc_epic(self, rfc_url: str) -> str | None:
        """Return the container-issue key recorded for this RFC URL, or None."""
        entry = self._data["rfc_tracking"].get(rfc_url, {})
        return entry.get("epic_key") or None

    def record_rfc_epic(self, rfc_url: str, epic_key: str) -> None:
        """Record the container issue tracking an RFC; refreshes tracked_at. No-op in read-only mode."""
        if self._read_only:
            return
        entry = self._data["rfc_tracking"].setdefault(rfc_url, {})
        entry["epic_key"] = epic_key
        entry["tracked_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def is_low_conf_pinged(self, pr_url: str) -> bool:
        """Check if a low-confidence email was already sent for this PR."""
        return pr_url in self._data["low_conf_pings"]

    def record_low_conf_ping(self, pr_url: str) -> None:
        """Record that a low-confidence email went out. No-op in read-only mode."""
        if self._read_only:
            return
        self._data["low_conf_pings"][pr_url] = {
            "pinged_at": datetime.now(timezone.utc).isoformat()
        }
        self._save()

    @staticmethod
    def _cancel_key(pr_url: str, ticket_key: str) -> str:
        return f"{pr_url}::{ticket_key}::cancel"

    def record_pr_cancel_seen(self, pr_url: str, ticket_key: str) -> bool:
        """Debounce closing a cancelled PR's ticket.

        Returns True once the PR has been observed closed-unmerged on a prior run
        (safe to close now); False on the first observation, so a PR that is closed
        then quickly reopened (CI re-trigger, stacked-PR tooling) never closes its
        ticket. No-op returning False in read-only mode.
        """
        if self._read_only:
            return False
        key = self._cancel_key(pr_url, ticket_key)
        if self._data["locks"].get(key, {}).get("cancel_seen_at"):
            return True
        now = datetime.now(timezone.utc).isoformat()
        entry = self._data["locks"].setdefault(key, {"locked_at": now})
        entry["cancel_seen_at"] = now
        self._save()
        return False

    def clear_pr_cancel_seen(self, pr_url: str, ticket_key: str) -> None:
        """Reset the cancel debounce once the PR is active or merged again."""
        if self._read_only:
            return
        key = self._cancel_key(pr_url, ticket_key)
        if key in self._data["locks"]:
            del self._data["locks"][key]
            self._save()

    def record_cancel_commented(self, pr_url: str, ticket_key: str) -> bool:
        """Dedup the honest cancel comment independently of the normal link comment.

        Returns True the first time (caller should post the note), False afterwards.
        Keyed on the cancel lock so a prior Review/In-Progress comment on the same
        PR can't suppress it, and a transition retry can't re-post it. Cleared with
        the debounce when the PR revives. No-op returning False in read-only mode.
        """
        if self._read_only:
            return False
        key = self._cancel_key(pr_url, ticket_key)
        now = datetime.now(timezone.utc).isoformat()
        entry = self._data["locks"].setdefault(key, {"locked_at": now})
        if entry.get("cancel_commented_at"):
            return False
        entry["cancel_commented_at"] = now
        self._save()
        return True

    def is_issue_processed(self, issue_url: str) -> bool:
        """Check if a GitHub Issue has already been classified."""
        return issue_url in self._data["classifications"]

    def is_issue_not_claiming(self, issue_url: str) -> bool:
        """Check if the issue's stored classification is a not_claiming
        comment verdict with no ticket attached."""
        entry = self._data["classifications"].get(issue_url)
        return (
            entry is not None
            and entry.get("intent") == "not_claiming"
            and not entry.get("ticket_key")
        )

    def record_issue_classification(
        self, issue_url: str, intent: str, reason: str
    ) -> None:
        """Record a claim classification result. No-op in read-only mode."""
        if self._read_only:
            return
        self._data["classifications"][issue_url] = {
            "intent": intent,
            "reason": reason,
            "classified_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def set_issue_ticket(self, issue_url: str, ticket_key: str) -> None:
        """Attach the resulting ticket key to a previously-classified issue."""
        if self._read_only or not ticket_key:
            return
        entry = self._data["classifications"].get(issue_url)
        if entry is None:
            return
        entry["ticket_key"] = ticket_key
        self._save()

    def get_pr_state_snapshot(self, ticket_key: str) -> dict | None:
        """Return the most recent PR-state snapshot for a ticket, or None."""
        snapshot = self._data["pr_state_snapshots"].get(ticket_key)
        return snapshot if isinstance(snapshot, dict) else None

    def record_pr_state_snapshot(
        self,
        ticket_key: str,
        pr_state: str,
        pr_url: str,
        resolved_status: str,
    ) -> None:
        """Persist the resolved PR state for a ticket. No-op in read-only mode."""
        if self._read_only:
            return
        self._data["pr_state_snapshots"][ticket_key] = {
            "pr_state": pr_state,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "pr_url": pr_url,
            "resolved_status": resolved_status,
        }
        self._save()

    @staticmethod
    def _co_key(pr_url: str, ticket_key: str, login: str) -> str:
        return f"{pr_url}::{ticket_key}::{login.lower()}"

    def is_co_contribution_noted(
        self, pr_url: str, ticket_key: str, login: str
    ) -> bool:
        """Check if a co-author note for this PR/ticket/login was already posted."""
        return self._co_key(pr_url, ticket_key, login) in self._data["co_contributions"]

    def record_co_contribution(self, pr_url: str, ticket_key: str, login: str) -> None:
        """Record a posted co-author note. No-op in read-only mode."""
        if self._read_only:
            return
        self._data["co_contributions"][self._co_key(pr_url, ticket_key, login)] = {
            "noted_at": datetime.now(timezone.utc).isoformat(),
            "github_user": login,
        }
        self._save()

    def record_digest_event(
        self,
        kind: str,
        *,
        ticket_key: str = "",
        pr_url: str = "",
        issue_url: str = "",
        github_user: str = "",
        old_value: str = "",
        new_value: str = "",
    ) -> None:
        """Append an attributed event to the digest namespace (R11). No-op in read-only mode."""
        if self._read_only:
            return
        self._data["digest"]["events"].append(
            {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "ticket_key": ticket_key,
                "pr_url": pr_url,
                "issue_url": issue_url,
                "github_user": github_user,
                "old_value": old_value,
                "new_value": new_value,
            }
        )
        self._save()

    def read_digest_events(self, since_iso: str) -> list[dict]:
        """Digest events observed at or after since_iso, oldest first."""
        return [
            e
            for e in self._data["digest"]["events"]
            if e.get("observed_at", "") >= since_iso
        ]

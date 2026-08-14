from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

import requests

from upstream_jira_sync.adf import AdfBuilder, adf_to_text
from upstream_jira_sync.http import BaseHTTPClient
from upstream_jira_sync.models import (
    JIRA_NO_NOTIFY,
    JiraTicket,
    JiraTicketChange,
    PullRequest,
    SprintRef,
    canonical_github_url,
    sanitize_identifier,
)

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z0-9_.:]{2,}")

RFC_EPIC_GLOBAL_ID_PREFIX = "rfc-epic::"


def _jql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class JiraClient(BaseHTTPClient):
    """Wraps the Jira Cloud REST API v3."""

    # Class defaults so __new__ test instances work without __init__ (R8: no
    # hardcoded field ids). _team_field is assignable by the wiring layer when
    # team assignment needs ticket.team_id populated from searches.
    _sprint_field: str = ""
    _team_field: str = ""
    _container_issue_type: str = "Epic"
    _exclude_containers: str = 'AND issuetype != "Epic"'
    _only_containers: str = 'AND issuetype = "Epic"'
    _open_status_clause: str = 'AND statusCategory in ("To Do", "In Progress")'
    _exclude_opt_out: str = ""

    def __init__(
        self,
        url: str,
        email: str,
        token: str,
        *,
        sprint_field: str = "",
        container_issue_type: str = "Epic",
        open_status_names: tuple[str, ...] = (),
        automation_opt_out_labels: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self._base = url.rstrip("/")
        self._sprint_field = sprint_field
        self._user_cache: dict[str, dict | None] = {}
        self._container_issue_type = container_issue_type
        # Candidate-pool filter: the configured status_map names when provided
        # (config.active_status_names), else the two real Jira status categories.
        if open_status_names:
            quoted = ", ".join(f'"{_jql_escape(s)}"' for s in open_status_names)
            self._open_status_clause = f"AND status in ({quoted})"
        # PR-driven sync operates on child issues; container issues (e.g. Epics)
        # are managed by RfcEpicTracker and are excluded from every candidate-pool
        # query. The container-only queries use _only_containers instead.
        escaped_type = _jql_escape(container_issue_type)
        self._exclude_containers = f'AND issuetype != "{escaped_type}"'
        self._only_containers = f'AND issuetype = "{escaped_type}"'
        # Opt-out labels keep human-owned cards out of the sprint sweep. The
        # `labels IS EMPTY` arm is required: in JQL a NOT IN predicate never
        # matches an issue whose multi-value field is empty, so without it an
        # unlabelled ticket would be filtered out of its own candidate pool.
        if automation_opt_out_labels:
            quoted = ", ".join(f'"{_jql_escape(x)}"' for x in automation_opt_out_labels)
            self._exclude_opt_out = (
                f"AND (labels IS EMPTY OR labels NOT IN ({quoted})) "
            )
        self._transition_target_ids: dict[str, str] = {}
        self._session.auth = (email, token)
        self._session.headers.update({"Content-Type": "application/json"})

    # -- Read operations -------------------------------------------------------

    def get_open_tickets(
        self, assignee_email: str, project_key: str | None = None
    ) -> list[JiraTicket]:
        """Return open / in-progress non-container tickets assigned to assignee_email.

        Container issues are excluded: PR-driven sync matches and transitions child
        issues, never the container itself (containers are managed by RfcEpicTracker).
        The one operation that does touch containers, team-field backfill, uses
        get_open_containers instead.
        """
        return self._get_open_assigned(
            assignee_email, project_key, self._exclude_containers
        )

    def get_open_containers(
        self, assignee_email: str, project_key: str | None = None
    ) -> list[JiraTicket]:
        """Return open container issues assigned to assignee_email. Used only to
        backfill the native Team field/labels (RfcEpicTracker creates containers
        team-less); never for PR matching or status transitions. Remote links are
        skipped: containers classify from their own text."""
        return self._get_open_assigned(
            assignee_email, project_key, self._only_containers, fetch_remote_links=False
        )

    def _get_open_assigned(
        self,
        assignee_email: str,
        project_key: str | None,
        issuetype_clause: str,
        *,
        fetch_remote_links: bool = True,
    ) -> list[JiraTicket]:
        email = sanitize_identifier(assignee_email, "jira_email")
        scope = f'project = "{project_key}" AND ' if project_key else ""
        jql = (
            f'{scope}assignee = "{email}" '
            f"{self._open_status_clause} "
            f"{issuetype_clause} "
            f"ORDER BY updated DESC"
        )
        tickets = self._search_tickets(jql, max_results=50, include_description=True)
        log.info("  Jira: found %d open issue(s) for assignee", len(tickets))
        if fetch_remote_links:
            for t in tickets:
                t.remote_links = self._get_remote_link_urls(t.key)
        return tickets

    def get_sprint_sweep_candidates(
        self,
        assignee_email: str,
        project_key: str,
        statuses: list[str],
        since_iso: str,
    ) -> list[JiraTicket]:
        """Open cards eligible for the sprint sweep: in an active status AND
        having entered it (or been created) on/after since_iso. Recency is
        answered by Jira's indexed history, not by parsing the changelog.
        Cards carrying an automation opt-out label are excluded outright."""
        email = sanitize_identifier(assignee_email, "jira_email")
        status_list = ", ".join(f'"{s}"' for s in statuses)
        jql = (
            f'project = "{project_key}" AND assignee = "{email}" '
            f"AND status in ({status_list}) "
            f'AND (status CHANGED TO ({status_list}) AFTER "{since_iso}" '
            f'OR created >= "{since_iso}") '
            f"{self._exclude_opt_out}"
            f"{self._exclude_containers} "
            f"ORDER BY updated DESC"
        )
        return self._search_tickets(jql, max_results=50)

    def _get_remote_link_urls(self, ticket_key: str) -> list[str]:
        """Fetch all remote link URLs for a ticket. Empty list on failure."""
        try:
            links = self._request(
                "GET",
                f"{self._base}/rest/api/3/issue/{ticket_key}/remotelink",
            ).json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("  Failed to fetch remote links for %s (%s)", ticket_key, exc)
            return []
        return [
            link.get("object", {}).get("url", "")
            for link in links
            if link.get("object", {}).get("url")
        ]

    def find_tracking_ticket(
        self, issue_url: str, project_key: str
    ) -> JiraTicket | None:
        """Find the ticket tracking a GitHub issue/PR URL.

        Primary: formal remote link (exact globalId match).
        Fallback: summary/description text search; on hit, backfill a remote link.
        """
        canonical = canonical_github_url(issue_url)

        try:
            primary = self._find_by_remote_link(canonical, project_key)
            if primary:
                return primary
            fallback = self._find_by_summary_description(canonical, project_key)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("  Tracking-ticket lookup failed for %s (%s)", canonical, exc)
            return None

        if fallback:
            relationship = "GitHub issue" if "/issues/" in canonical else "pull request"
            try:
                self.add_remote_link(
                    fallback,
                    canonical,
                    f"GitHub {canonical}",
                    relationship=relationship,
                )
            except Exception:
                log.exception("  Failed to backfill remote link on %s", fallback.key)
            return fallback

        return None

    def _find_by_remote_link(
        self, canonical_url: str, project_key: str
    ) -> JiraTicket | None:
        escaped = _jql_escape(canonical_url)
        jql = (
            f'project = "{project_key}" '
            f'AND issue in issuesWithRemoteLinksByGlobalId("{escaped}") '
            f"{self._exclude_containers}"
        )
        return self._first_ticket_from_jql(jql)

    def _find_by_summary_description(
        self, canonical_url: str, project_key: str
    ) -> JiraTicket | None:
        escaped = _jql_escape(canonical_url)
        jql = (
            f'project = "{project_key}" '
            f'AND (summary ~ "\\"{escaped}\\"" OR description ~ "\\"{escaped}\\"") '
            f"{self._exclude_containers}"
        )
        return self._first_ticket_from_jql(jql)

    def _first_ticket_from_jql(self, jql: str) -> JiraTicket | None:
        results = self._search_tickets(jql, max_results=1)
        return results[0] if results else None

    def find_epic_for_rfc(self, rfc_url: str, project_key: str) -> JiraTicket | None:
        """Find the container issue tracking this RFC via its namespaced remote
        link. Container issuetype only."""
        global_id = RFC_EPIC_GLOBAL_ID_PREFIX + canonical_github_url(rfc_url)
        escaped = _jql_escape(global_id)
        jql = (
            f'project = "{project_key}" '
            f'AND issue in issuesWithRemoteLinksByGlobalId("{escaped}") '
            f"{self._only_containers}"
        )
        try:
            ticket = self._first_ticket_from_jql(jql)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("  RFC epic lookup failed for %s (%s)", rfc_url, exc)
            return None
        if ticket is None or not ticket.is_type(self._container_issue_type):
            return None
        return ticket

    def find_candidate_tickets(
        self, project_key: str, title: str, max_results: int = 15
    ) -> list[JiraTicket]:
        """Project tickets whose summary/description fuzzily matches the words of `title`.

        Candidate pool for the AI dedup check; any status, any assignee, child issues only.
        """
        words = " ".join(_WORD_RE.findall(title))
        if not words:
            return []
        jql = (
            f'project = "{project_key}" '
            f'AND text ~ "{_jql_escape(words)}" '
            f"{self._exclude_containers} ORDER BY updated DESC"
        )
        try:
            return self._search_tickets(jql, max_results=max_results)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning("  Candidate-ticket search failed for dedup (%s)", exc)
            return []

    def search_ticket_changes(
        self,
        jql: str,
        since_iso: str,
        story_points_field: str,
        max_results: int = 100,
    ) -> list[JiraTicketChange]:
        """Emits one change per status/story-points edit, plus a synthetic `Created` entry for in-window ticket creations."""
        resp = self._request(
            "GET",
            f"{self._base}/rest/api/3/search/jql",
            params={
                "jql": jql,
                "fields": f"summary,status,created,assignee,{story_points_field}",
                "expand": "changelog",
                "maxResults": max_results,
            },
        ).json()

        tracked = {"status", story_points_field, "Story Points", "storyPoints"}
        changes: list[JiraTicketChange] = []

        for issue in resp.get("issues", []):
            key = issue["key"]
            fields = issue.get("fields", {}) or {}
            summary = fields.get("summary", "")
            created = fields.get("created", "")
            assignee_email = (fields.get("assignee") or {}).get(
                "emailAddress", ""
            ) or ""

            if created and created >= since_iso:
                changes.append(
                    JiraTicketChange(
                        ticket_key=key,
                        ticket_summary=summary,
                        field="Created",
                        from_value="",
                        to_value=summary,
                        changed_at=created,
                        author_email="",
                        ticket_assignee_email=assignee_email,
                    )
                )

            for hist in issue.get("changelog", {}).get("histories", []):
                when = hist.get("created", "")
                if when < since_iso:
                    continue
                author_email = (hist.get("author") or {}).get("emailAddress", "") or ""
                for item in hist.get("items", []):
                    field = item.get("field") or item.get("fieldId") or ""
                    if field not in tracked and item.get("fieldId") not in tracked:
                        continue
                    changes.append(
                        JiraTicketChange(
                            ticket_key=key,
                            ticket_summary=summary,
                            field=field,
                            from_value=item.get("fromString") or "",
                            to_value=item.get("toString") or "",
                            changed_at=when,
                            author_email=author_email,
                            ticket_assignee_email=assignee_email,
                        )
                    )

        return changes

    def _search_tickets(
        self,
        jql: str,
        max_results: int,
        include_description: bool = False,
    ) -> list[JiraTicket]:
        """Run a JQL search and return basic JiraTickets (no remote_links)."""
        base_fields = "summary,status,labels,parent,issuetype"
        if include_description:
            base_fields += ",description"
        if self._sprint_field:
            base_fields += f",{self._sprint_field}"
        if self._team_field:
            base_fields += f",{self._team_field}"
        issues: list[dict] = (
            self._request(
                "GET",
                f"{self._base}/rest/api/3/search/jql",
                params={
                    "jql": jql,
                    "fields": base_fields,
                    "maxResults": max_results,
                },
            )
            .json()
            .get("issues", [])
        )
        return [
            JiraTicket(
                key=i["key"],
                summary=i["fields"]["summary"],
                status=i["fields"]["status"]["name"],
                url=f"{self._base}/browse/{i['key']}",
                description=adf_to_text(i["fields"].get("description"))
                if include_description
                else "",
                labels=list(i["fields"].get("labels") or []),
                parent_key=(i["fields"].get("parent") or {}).get("key", ""),
                issuetype=(i["fields"].get("issuetype") or {}).get("name", ""),
                sprint_ids={
                    s["id"] for s in (i["fields"].get(self._sprint_field) or [])
                }
                if self._sprint_field
                else set(),
                team_id=(i["fields"].get(self._team_field) or {}).get("id", "")
                if self._team_field
                else "",
            )
            for i in issues
        ]

    def get_sprint_by_name(self, board_id: int, name: str) -> SprintRef | None:
        """Find the sprint named `name` (exact) on board_id, any state, or None."""
        url = f"{self._base}/rest/agile/1.0/board/{board_id}/sprint"
        start_at = 0
        try:
            while True:
                data = self._request(
                    "GET",
                    url,
                    params={"state": "active,future,closed", "startAt": start_at},
                ).json()
                values = data.get("values", [])
                for s in values:
                    if s["name"] == name:
                        return SprintRef(id=s["id"], name=s["name"])
                if data.get("isLast", True) or not values:
                    return None
                start_at += len(values)
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.warning(
                "  Sprint lookup failed for %r on board %d (%s)", name, board_id, exc
            )
            return None

    def add_issues_to_sprint(self, sprint_id: int, issue_keys: list[str]) -> None:
        """Move issues onto the native sprint (max 50/call). HTTPError propagates."""
        self._request(
            "POST",
            f"{self._base}/rest/agile/1.0/sprint/{sprint_id}/issue",
            json={"issues": issue_keys},
        )
        log.info("  Sprint %d: added %d issue(s)", sprint_id, len(issue_keys))

    def create_sprint(
        self, board_id: int, name: str, start: date, end: date
    ) -> SprintRef:
        """Create a future sprint on board_id over [start, end). HTTPError propagates."""
        data = self._request(
            "POST",
            f"{self._base}/rest/agile/1.0/sprint",
            json={
                "name": name,
                "originBoardId": board_id,
                "startDate": f"{start.isoformat()}T00:00:00.000Z",
                "endDate": f"{end.isoformat()}T00:00:00.000Z",
            },
        ).json()
        log.info("  Created sprint %r (id %s)", name, data.get("id"))
        return SprintRef(id=data["id"], name=data["name"])

    # -- Write operations ------------------------------------------------------

    def create_ticket(
        self,
        project_key: str,
        summary: str,
        description_adf: dict,
        assignee_email: str,
        *,
        labels: list[str] | None = None,
        extra_fields: dict | None = None,
        issuetype: str = "Story",
        parent_key: str | None = None,
        components: list[str] | None = None,
        initial_status_name: str = "To Do",
    ) -> JiraTicket:
        """Create a new Jira issue assigned to the claiming engineer."""
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": {"name": issuetype},
            "summary": summary[:255],
            "description": description_adf,
        }
        if assignee_email:
            users = self._request(
                "GET",
                f"{self._base}/rest/api/3/user/search",
                params={"query": assignee_email, "maxResults": 1},
            ).json()
            if users and users[0].get("accountId"):
                fields["assignee"] = {"accountId": users[0]["accountId"]}

        if parent_key:
            fields["parent"] = {"key": parent_key}

        if components:
            fields["components"] = [{"name": c} for c in components]

        extras = dict(extra_fields) if extra_fields else {}
        extra_labels = (
            extras.pop("labels", None)
            if isinstance(extras.get("labels"), list)
            else None
        )
        if extras:
            fields.update(extras)

        merged_labels: list[str] = []
        if labels:
            merged_labels.extend(labels)
        if extra_labels:
            merged_labels.extend(extra_labels)
        if merged_labels:
            fields["labels"] = merged_labels

        resp = self._request(
            "POST",
            f"{self._base}/rest/api/3/issue",
            params=JIRA_NO_NOTIFY,
            json={"fields": fields},
        )
        data = resp.json()
        key = data["key"]
        log.info("  Created %s: %s", key, summary[:60])
        return JiraTicket(
            key=key,
            summary=summary,
            status=initial_status_name,
            url=f"{self._base}/browse/{key}",
        )

    def set_story_points(
        self,
        ticket: JiraTicket,
        points: int,
        field_name: str,
    ) -> None:
        """Set story points on a ticket via the configured custom field."""
        self._request(
            "PUT",
            f"{self._base}/rest/api/3/issue/{ticket.key}",
            params=JIRA_NO_NOTIFY,
            json={"fields": {field_name: float(points)}},
        )
        log.info("  %s: set story points to %d", ticket.key, points)

    def update_labels(self, ticket_key: str, labels: list[str]) -> None:
        """Replace labels on the ticket. notifyUsers=false to avoid email spam."""
        self._request(
            "PUT",
            f"{self._base}/rest/api/3/issue/{ticket_key}",
            params=JIRA_NO_NOTIFY,
            json={"fields": {"labels": labels}},
        )
        log.info("  %s: set labels to %s", ticket_key, labels)

    def set_team(self, ticket_key: str, team_id: str, team_field: str) -> None:
        """Set the native Team field. notifyUsers=false.

        The Atlassian Team field takes the bare team-id string, not an {"id": ...} object.
        """
        self._request(
            "PUT",
            f"{self._base}/rest/api/3/issue/{ticket_key}",
            params=JIRA_NO_NOTIFY,
            json={"fields": {team_field: team_id}},
        )
        log.info("  %s: set team to %s", ticket_key, team_id)

    def _resolve_user(self, email: str) -> dict | None:
        """Look up a Jira user by email. Failed lookups are cached as None so
        they are not retried within the run."""
        key = email.lower()
        if key in self._user_cache:
            return self._user_cache[key]
        user: dict | None = None
        try:
            users = self._request(
                "GET",
                f"{self._base}/rest/api/3/user/search",
                params={"query": email},
            ).json()
            matched = [u for u in users if (u.get("emailAddress") or "").lower() == key]
            if matched:
                user = matched[0]
            elif len(users) == 1:
                user = users[0]
        except Exception:
            log.warning("  Could not resolve Jira account for %s", email)
        self._user_cache[key] = user
        return user

    def resolve_account_id(self, email: str) -> str | None:
        return (self._resolve_user(email) or {}).get("accountId")

    def resolve_display_name(self, email: str) -> str:
        return (self._resolve_user(email) or {}).get("displayName", "")

    def add_contributor(self, ticket_key: str, account_id: str, field_id: str) -> None:
        """Append a user to a multi-user picker field via the update verb: no
        read-modify-write race, no clobbering manual entries. notifyUsers=false."""
        self._request(
            "PUT",
            f"{self._base}/rest/api/3/issue/{ticket_key}",
            params=JIRA_NO_NOTIFY,
            json={"update": {field_id: [{"add": {"accountId": account_id}}]}},
        )
        log.info("  %s: added contributor %s", ticket_key, account_id)

    def set_parent(self, ticket_key: str, parent_key: str) -> None:
        """Set the parent (container issue) of an issue. notifyUsers=false."""
        self._request(
            "PUT",
            f"{self._base}/rest/api/3/issue/{ticket_key}",
            params=JIRA_NO_NOTIFY,
            json={"fields": {"parent": {"key": parent_key}}},
        )
        log.info("  %s: set parent to %s", ticket_key, parent_key)

    def post_comment(
        self,
        ticket: JiraTicket,
        pr: PullRequest,
        status_name: str,
        match_confidence: str = "",
        match_reason: str = "",
        note: str = "",
    ) -> None:
        """Post an ADF-formatted PR link comment to ticket."""
        payload = AdfBuilder.pr_comment(
            pr,
            status_name,
            match_confidence=match_confidence,
            match_reason=match_reason,
            note=note,
        )
        self._request(
            "POST",
            f"{self._base}/rest/api/3/issue/{ticket.key}/comment",
            params=JIRA_NO_NOTIFY,
            json=payload,
        )
        log.info("  Commented on %s -- PR #%d", ticket.key, pr.number)

    def post_note(self, ticket: JiraTicket, text: str) -> None:
        """Post a plain-text comment (no PR context). notifyUsers=false."""
        self._request(
            "POST",
            f"{self._base}/rest/api/3/issue/{ticket.key}/comment",
            params=JIRA_NO_NOTIFY,
            json=AdfBuilder.note(text),
        )
        log.info("  Noted on %s: %s", ticket.key, text[:60])

    def post_mention_note(
        self,
        ticket: JiraTicket,
        before: str,
        account_id: str,
        after: str,
        display_name: str = "",
    ) -> None:
        """Post a one-paragraph comment with a real user mention. notifyUsers=false."""
        self._request(
            "POST",
            f"{self._base}/rest/api/3/issue/{ticket.key}/comment",
            params=JIRA_NO_NOTIFY,
            json=AdfBuilder.mention_note(before, account_id, after, display_name),
        )
        log.info(
            "  Noted on %s: %s@%s%s",
            ticket.key,
            before,
            display_name or account_id,
            after[:40],
        )

    def add_remote_link(
        self,
        ticket: JiraTicket,
        url: str,
        title: str,
        relationship: str = "pull request",
        *,
        global_id: str = "",
    ) -> None:
        """Add a web link to a Jira ticket via the remote link API."""
        canonical = canonical_github_url(url)
        self._request(
            "POST",
            f"{self._base}/rest/api/3/issue/{ticket.key}/remotelink",
            params=JIRA_NO_NOTIFY,
            json={
                "globalId": global_id or canonical,
                "application": {"type": "com.github", "name": "GitHub"},
                "relationship": relationship,
                "object": {
                    "url": canonical,
                    "title": title,
                    "icon": {
                        "url16x16": "https://github.com/favicon.ico",
                        "title": f"GitHub {relationship.title()}",
                    },
                },
            },
        )
        log.info("  %s: linked %s (%s)", ticket.key, url, relationship)

    def transition_ticket(
        self,
        ticket: JiraTicket,
        target_status_name: str,
    ) -> bool:
        """Transition ticket to the status named target_status_name, matching on
        the transition's TARGET status (to.name / cached to.id), never on the
        transition's own label (R4)."""
        if ticket.is_status(target_status_name):
            log.info(
                "  %s already in '%s', no transition needed.",
                ticket.key,
                ticket.status,
            )
            return False

        transitions: list[dict] = (
            self._request(
                "GET",
                f"{self._base}/rest/api/3/issue/{ticket.key}/transitions",
                params={"expand": "transitions.fields"},
            )
            .json()
            .get("transitions", [])
        )

        target = target_status_name.lower()
        match = None
        cached_id = self._transition_target_ids.get(target)
        if cached_id:
            match = next(
                (t for t in transitions if (t.get("to") or {}).get("id") == cached_id),
                None,
            )
        if match is None:
            match = next(
                (
                    t
                    for t in transitions
                    if (t.get("to") or {}).get("name", "").lower() == target
                ),
                None,
            )

        if not match:
            available = [(t.get("to") or {}).get("name", "") for t in transitions]
            log.warning(
                "  No transition to '%s' for %s. Available: %s",
                target_status_name,
                ticket.key,
                available,
            )
            return False

        to_id = (match.get("to") or {}).get("id", "")
        if to_id:
            self._transition_target_ids[target] = to_id

        self._request(
            "POST",
            f"{self._base}/rest/api/3/issue/{ticket.key}/transitions",
            params=JIRA_NO_NOTIFY,
            json={"transition": {"id": match["id"]}},
        )
        log.info("  %s: '%s' -> '%s'", ticket.key, ticket.status, target_status_name)
        return True


class DryRunJiraClient(JiraClient):
    """Drop-in replacement for JiraClient that logs what it would do."""

    def set_story_points(
        self,
        ticket: JiraTicket,
        points: int,
        field_name: str,
    ) -> None:
        log.info(
            "  [DRY RUN] Would set %s story points to %d",
            ticket.key,
            points,
        )

    def update_labels(self, ticket_key: str, labels: list[str]) -> None:
        log.info("  [DRY RUN] Would set %s labels to %s", ticket_key, labels)

    def set_team(self, ticket_key: str, team_id: str, team_field: str) -> None:
        log.info("  [DRY RUN] Would set %s team to %s", ticket_key, team_id)

    def add_issues_to_sprint(self, sprint_id: int, issue_keys: list[str]) -> None:
        log.info(
            "  [DRY RUN] Would add %d issue(s) to sprint %d: %s",
            len(issue_keys),
            sprint_id,
            issue_keys,
        )

    def create_sprint(
        self, board_id: int, name: str, start: date, end: date
    ) -> SprintRef:
        log.info(
            "  [DRY RUN] Would create sprint %r on board %d (%s to %s)",
            name,
            board_id,
            start.isoformat(),
            end.isoformat(),
        )
        return SprintRef(id=0, name=name)

    def add_contributor(self, ticket_key: str, account_id: str, field_id: str) -> None:
        log.info(
            "  [DRY RUN] Would add contributor %s to %s (%s)",
            account_id,
            ticket_key,
            field_id,
        )

    def set_parent(self, ticket_key: str, parent_key: str) -> None:
        log.info("  [DRY RUN] Would set %s parent to %s", ticket_key, parent_key)

    def post_comment(
        self,
        ticket: JiraTicket,
        pr: PullRequest,
        status_name: str,
        match_confidence: str = "",
        match_reason: str = "",
        note: str = "",
    ) -> None:
        log.info(
            "  [DRY RUN] Would comment on %s -- PR #%d (%s)",
            ticket.key,
            pr.number,
            status_name,
        )

    def post_note(self, ticket: JiraTicket, text: str) -> None:
        log.info("  [DRY RUN] Would note on %s: %s", ticket.key, text[:60])

    def post_mention_note(
        self,
        ticket: JiraTicket,
        before: str,
        account_id: str,
        after: str,
        display_name: str = "",
    ) -> None:
        log.info(
            "  [DRY RUN] Would note on %s: %s@%s%s",
            ticket.key,
            before,
            display_name or account_id,
            after[:40],
        )

    def add_remote_link(
        self,
        ticket: JiraTicket,
        url: str,
        title: str,
        relationship: str = "pull request",
        *,
        global_id: str = "",
    ) -> None:
        log.info(
            "  [DRY RUN] Would link %s -> %s (%s)",
            ticket.key,
            url,
            relationship,
        )

    def transition_ticket(
        self,
        ticket: JiraTicket,
        target_status_name: str,
    ) -> bool:
        if ticket.is_status(target_status_name):
            log.info(
                "  [DRY RUN] %s already in '%s', no transition needed.",
                ticket.key,
                ticket.status,
            )
            return False

        log.info(
            "  [DRY RUN] Would transition %s: '%s' -> '%s'",
            ticket.key,
            ticket.status,
            target_status_name,
        )
        return True

    def create_ticket(
        self,
        project_key: str,
        summary: str,
        description_adf: dict,
        assignee_email: str,
        *,
        labels: list[str] | None = None,
        extra_fields: dict | None = None,
        issuetype: str = "Story",
        parent_key: str | None = None,
        components: list[str] | None = None,
        initial_status_name: str = "To Do",
    ) -> JiraTicket:
        log.info(
            "  [DRY RUN] Would create %s in %s: %s (labels: %s, parent: %s, "
            "components: %s, extra_fields: %s)",
            issuetype,
            project_key,
            summary[:60],
            labels or [],
            parent_key or "",
            components or [],
            extra_fields or {},
        )
        return JiraTicket(
            key=f"{project_key}-DRYRUN",
            summary=summary,
            status=initial_status_name,
            url=f"https://dryrun/{project_key}-DRYRUN",
        )

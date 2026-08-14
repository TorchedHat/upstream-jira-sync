"""JiraClient, DryRunJiraClient, AdfBuilder, and URL/model helpers."""

import json
from unittest.mock import MagicMock

import pytest
import requests
from conftest import make_pr, make_ticket
from upstream_jira_sync.adf import AdfBuilder, adf_to_text
from upstream_jira_sync.jira import DryRunJiraClient, JiraClient
from upstream_jira_sync.models import (
    SprintRef,
    canonical_github_url,
    sanitize_identifier,
)


def _client(**kwargs) -> JiraClient:
    client = JiraClient("https://jira.test", "bot@example.com", "jtok", **kwargs)
    client._request = MagicMock()
    return client


def test_open_status_clause_prefers_configured_names_over_categories():
    named = _client(open_status_names=("New", "Doing", "Code Review"))
    assert named._open_status_clause == 'AND status in ("New", "Doing", "Code Review")'
    default = _client()
    assert (
        default._open_status_clause == 'AND statusCategory in ("To Do", "In Progress")'
    )


def _resp(payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _search_hit(key="PROJ-1", status="To Do"):
    return _resp(
        {
            "issues": [
                {"key": key, "fields": {"summary": "x", "status": {"name": status}}}
            ]
        }
    )


class TestModelHelpers:
    def test_sanitize_identifier_valid(self):
        assert (
            sanitize_identifier("octocat@example.com", "email") == "octocat@example.com"
        )

    def test_sanitize_identifier_injection_raises(self):
        with pytest.raises(ValueError, match="Invalid characters"):
            sanitize_identifier('user" OR 1=1 --', "github")
        with pytest.raises(ValueError, match="Invalid characters"):
            sanitize_identifier("", "field")

    def test_canonical_github_url(self):
        assert (
            canonical_github_url("https://GitHub.com/org/repo/issues/1/?x=1#a")
            == "https://github.com/org/repo/issues/1"
        )
        assert canonical_github_url(
            "https://github.com/org/repo/issues/1"
        ) != canonical_github_url("https://github.com/org/repo/pull/1")


class TestAdfBuilder:
    def test_pr_comment_structure(self):
        payload = AdfBuilder.pr_comment(make_pr(number=512), "In Review")
        body = payload["body"]
        assert body["type"] == "doc"
        assert body["version"] == 1
        assert len(body["content"]) == 2
        assert "In Review" in json.dumps(body)

    def test_comment_includes_reasoning_and_note(self):
        payload = AdfBuilder.pr_comment(
            make_pr(),
            "In Review",
            match_confidence="high",
            match_reason="Title matches ticket goal",
            note="PR closed without merge -- ticket closed as cancelled.",
        )
        content = payload["body"]["content"]
        assert len(content) == 4
        assert "high" in content[2]["content"][0]["text"]
        assert "closed without merge" in content[3]["content"][0]["text"]

    def test_pr_description_structure(self):
        adf = AdfBuilder.pr_description(
            "Fix transport",
            "https://github.com/exampleorg/widgets/pull/5",
            "Adds a retry loop.",
        )
        assert adf["type"] == "doc"
        href = adf["content"][0]["content"][0]["marks"][0]["attrs"]["href"]
        assert href.endswith("/pull/5")
        assert "Adds a retry loop." in json.dumps(adf)

    def test_adf_to_text_extracts_nested_text(self):
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Lower view ops."}],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "redistribute path"}
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        text = adf_to_text(adf)
        assert "Lower view ops." in text
        assert "redistribute path" in text

    def test_adf_to_text_non_dict_returns_empty(self):
        assert adf_to_text(None) == ""
        assert adf_to_text("plain string") == ""


class TestCreateTicket:
    def test_payload_includes_all_kwargs(self):
        client = _client()
        client._request.side_effect = [
            _resp([{"accountId": "acct-1"}]),
            _resp({"key": "PROJ-42"}),
        ]

        ticket = client.create_ticket(
            "PROJ",
            "Container for RFC X",
            {"type": "doc", "version": 1, "content": []},
            "octocat@example.com",
            labels=["rfc", "tracker"],
            extra_fields={"customfield_12345": 5.0},
            issuetype="Initiative",
            parent_key="PROJ-1",
            initial_status_name="New",
        )

        assert ticket.key == "PROJ-42"
        assert ticket.status == "New"
        create_call = client._request.call_args_list[1]
        fields = create_call.kwargs["json"]["fields"]
        assert fields["issuetype"] == {"name": "Initiative"}
        assert fields["labels"] == ["rfc", "tracker"]
        assert fields["parent"] == {"key": "PROJ-1"}
        assert fields["customfield_12345"] == 5.0
        assert fields["assignee"] == {"accountId": "acct-1"}
        assert create_call.kwargs["params"] == {"notifyUsers": "false"}

    def test_sets_components_field(self):
        client = _client()
        client._request.side_effect = [_resp({"key": "PROJ-7"})]

        client.create_ticket(
            "PROJ",
            "T",
            {"type": "doc", "version": 1, "content": []},
            "",
            components=["Framework"],
        )

        fields = client._request.call_args_list[0].kwargs["json"]["fields"]
        assert fields["components"] == [{"name": "Framework"}]

    def test_merges_labels_from_kwarg_and_extra(self):
        client = _client()
        client._request.side_effect = [_resp({"key": "PROJ-99"})]

        caller_extras = {"labels": ["sprint-2026-06"]}
        client.create_ticket(
            "PROJ",
            "Mixed labels",
            {"type": "doc", "version": 1, "content": []},
            "",
            labels=["rfc"],
            extra_fields=caller_extras,
        )

        fields = client._request.call_args_list[0].kwargs["json"]["fields"]
        assert fields["labels"] == ["rfc", "sprint-2026-06"]
        assert caller_extras == {"labels": ["sprint-2026-06"]}


class TestSearchTicketParsing:
    def test_parses_labels_parent_issuetype(self):
        client = _client()
        client._request.return_value = _resp(
            {
                "issues": [
                    {
                        "key": "PROJ-1",
                        "fields": {
                            "summary": "Child story",
                            "status": {"name": "To Do"},
                            "labels": ["foo", "bar"],
                            "parent": {"key": "PROJ-9"},
                            "issuetype": {"name": "Story"},
                        },
                    },
                    {
                        "key": "PROJ-2",
                        "fields": {
                            "summary": "Bare ticket",
                            "status": {"name": "To Do"},
                            "labels": None,
                            "parent": None,
                            "issuetype": None,
                        },
                    },
                ]
            }
        )

        tickets = client._search_tickets("project = PROJ", max_results=10)

        assert tickets[0].labels == ["foo", "bar"]
        assert tickets[0].parent_key == "PROJ-9"
        assert tickets[0].issuetype == "Story"
        assert tickets[1].labels == []
        assert tickets[1].parent_key == ""
        assert tickets[1].issuetype == ""
        fields = client._request.call_args.kwargs["params"]["fields"]
        assert "labels" in fields and "parent" in fields and "issuetype" in fields

    def test_parses_sprint_and_team_fields_when_configured(self):
        client = _client(sprint_field="customfield_99001")
        client._team_field = "customfield_11111"
        client._request.return_value = _resp(
            {
                "issues": [
                    {
                        "key": "PROJ-1",
                        "fields": {
                            "summary": "On a sprint",
                            "status": {"name": "To Do"},
                            "customfield_99001": [
                                {"id": 68993, "name": "Sprint 35"},
                                {"id": 68000, "name": "Sprint 30"},
                            ],
                            "customfield_11111": {"id": "team-uuid-alpha"},
                        },
                    },
                    {
                        "key": "PROJ-2",
                        "fields": {
                            "summary": "No sprint",
                            "status": {"name": "To Do"},
                            "customfield_99001": None,
                            "customfield_11111": None,
                        },
                    },
                ]
            }
        )

        tickets = client._search_tickets("project = PROJ", max_results=10)

        assert tickets[0].sprint_ids == {68993, 68000}
        assert tickets[0].team_id == "team-uuid-alpha"
        assert tickets[1].sprint_ids == set()
        assert tickets[1].team_id == ""
        fields = client._request.call_args.kwargs["params"]["fields"]
        assert "customfield_99001" in fields and "customfield_11111" in fields

    def test_unconfigured_sprint_field_omitted_from_query(self):
        client = _client()
        client._request.return_value = _resp({"issues": []})
        client._search_tickets("project = PROJ", max_results=10)
        fields = client._request.call_args.kwargs["params"]["fields"]
        assert "customfield" not in fields


class TestContainerTypeClauses:
    """Issue-type JQL clauses are built from the configured container type (R5)."""

    def _jql_of(self, call, **client_kwargs):
        client = _client(**client_kwargs)
        client._request.return_value = _resp({"issues": []})
        call(client)
        return client._request.call_args.kwargs["params"]["jql"]

    def test_open_tickets_exclude_configured_container(self):
        jql = self._jql_of(
            lambda c: c.get_open_tickets("octocat@example.com", "PROJ"),
            container_issue_type="Theme",
        )
        assert 'issuetype != "Theme"' in jql
        assert "Epic" not in jql

    def test_open_containers_filter_to_configured_container(self):
        jql = self._jql_of(
            lambda c: c.get_open_containers("octocat@example.com", "PROJ"),
            container_issue_type="Theme",
        )
        assert 'issuetype = "Theme"' in jql
        assert 'issuetype != "Theme"' not in jql

    def test_default_container_type_is_epic(self):
        jql = self._jql_of(lambda c: c.get_open_tickets("octocat@example.com", "PROJ"))
        assert 'issuetype != "Epic"' in jql

    def test_tracking_sweep_and_dedup_queries_exclude_containers(self):
        for call in (
            lambda c: c.find_tracking_ticket(
                "https://github.com/exampleorg/widgets/pull/2", "PROJ"
            ),
            lambda c: c.get_sprint_sweep_candidates(
                "octocat@example.com", "PROJ", ["In Progress"], "2026-06-01"
            ),
            lambda c: c.find_candidate_tickets("PROJ", "Refactor transport layer"),
        ):
            jql = self._jql_of(call, container_issue_type="Theme")
            assert 'issuetype != "Theme"' in jql

    def test_sweep_excludes_opt_out_labels_without_dropping_unlabelled(self):
        jql = self._jql_of(
            lambda c: c.get_sprint_sweep_candidates(
                "octocat@example.com", "PROJ", ["In Progress"], "2026-06-01"
            ),
            automation_opt_out_labels=("no-automation", "no-sprint"),
        )
        assert 'labels IS EMPTY OR labels NOT IN ("no-automation", "no-sprint")' in jql

    def test_sweep_has_no_label_clause_when_unconfigured(self):
        jql = self._jql_of(
            lambda c: c.get_sprint_sweep_candidates(
                "octocat@example.com", "PROJ", ["In Progress"], "2026-06-01"
            )
        )
        assert "labels" not in jql

    def test_opt_out_clause_does_not_touch_pr_matching_pool(self):
        jql = self._jql_of(
            lambda c: c.get_open_tickets("octocat@example.com", "PROJ"),
            automation_opt_out_labels=("no-automation",),
        )
        assert "labels" not in jql


class TestTransitionTicket:
    """Transitions resolve by TARGET status (to.name / cached to.id), never by
    the transition's own label (R4)."""

    _PAYLOAD = {
        "transitions": [
            # Decoy: the transition LABEL matches the target name but it leads elsewhere.
            {"id": "11", "name": "In Review", "to": {"id": "500", "name": "Blocked"}},
            {
                "id": "21",
                "name": "Submit for review",
                "to": {"id": "400", "name": "In Review"},
            },
        ]
    }

    def test_matches_on_target_status_name_not_transition_label(self):
        client = _client()
        client._request.side_effect = [_resp(self._PAYLOAD), _resp({})]

        moved = client.transition_ticket(make_ticket("PROJ-1", "x"), "In Review")

        assert moved is True
        post = client._request.call_args_list[1]
        assert post.args[0] == "POST"
        assert post.kwargs["json"] == {"transition": {"id": "21"}}
        assert post.kwargs["params"] == {"notifyUsers": "false"}
        assert client._transition_target_ids["in review"] == "400"

    def test_cached_target_id_preferred_over_name(self):
        client = _client()
        client._transition_target_ids["done"] = "700"
        payload = {
            "transitions": [
                {"id": "1", "name": "Finish", "to": {"id": "600", "name": "Done"}},
                {"id": "2", "name": "Complete", "to": {"id": "700", "name": "Done"}},
            ]
        }
        client._request.side_effect = [_resp(payload), _resp({})]

        assert client.transition_ticket(make_ticket("PROJ-1", "x"), "Done") is True
        post = client._request.call_args_list[1]
        assert post.kwargs["json"] == {"transition": {"id": "2"}}

    def test_no_matching_target_returns_false(self):
        client = _client()
        client._request.side_effect = [
            _resp(
                {
                    "transitions": [
                        {"id": "1", "name": "Go", "to": {"id": "9", "name": "QA"}}
                    ]
                }
            )
        ]
        assert client.transition_ticket(make_ticket("PROJ-1", "x"), "Done") is False
        assert client._request.call_count == 1  # GET only, no POST

    def test_already_in_target_status_returns_false(self):
        client = _client()
        result = client.transition_ticket(
            make_ticket("PROJ-1", "x", status="In Review"), "In Review"
        )
        assert result is False
        client._request.assert_not_called()


class TestWriteOperations:
    def test_update_labels_calls_put(self):
        client = _client()
        client.update_labels("PROJ-100", ["sprint-2026-06", "rfc"])

        call = client._request.call_args
        assert call.args[0] == "PUT"
        assert call.args[1] == "https://jira.test/rest/api/3/issue/PROJ-100"
        assert call.kwargs["json"] == {"fields": {"labels": ["sprint-2026-06", "rfc"]}}
        assert call.kwargs["params"] == {"notifyUsers": "false"}

    def test_set_parent_calls_put(self):
        client = _client()
        client.set_parent("PROJ-100", "PROJ-9")
        call = client._request.call_args
        assert call.args[0] == "PUT"
        assert call.kwargs["json"] == {"fields": {"parent": {"key": "PROJ-9"}}}

    def test_set_team_writes_bare_id_to_configured_field(self):
        client = _client()
        client.set_team("PROJ-100", "team-uuid-alpha", "customfield_11111")
        call = client._request.call_args
        assert call.kwargs["json"] == {
            "fields": {"customfield_11111": "team-uuid-alpha"}
        }
        assert call.kwargs["params"] == {"notifyUsers": "false"}

    def test_set_story_points_writes_float_to_configured_field(self):
        client = _client()
        client.set_story_points(make_ticket("PROJ-1", "x"), 5, "customfield_12345")
        call = client._request.call_args
        assert call.kwargs["json"] == {"fields": {"customfield_12345": 5.0}}

    def test_remote_link_same_url_same_global_id(self):
        client = _client()
        url = "https://github.com/exampleorg/widgets/pull/99"
        ticket = make_ticket("PROJ-1", "x")
        client.add_remote_link(ticket, url, "PR #99")
        client.add_remote_link(ticket, url, "PR #99")

        calls = client._request.call_args_list
        gid1 = calls[0].kwargs["json"]["globalId"]
        gid2 = calls[1].kwargs["json"]["globalId"]
        assert gid1 == gid2
        assert url in gid1


class TestGetOpenTickets:
    def test_remote_links_populated(self):
        client = _client()
        client._request.side_effect = [
            _search_hit("PROJ-1"),
            _resp(
                [
                    {
                        "object": {
                            "url": "https://github.com/exampleorg/widgets/issues/1"
                        }
                    },
                    {"object": {"url": "https://github.com/exampleorg/widgets/pull/2"}},
                ]
            ),
        ]

        tickets = client.get_open_tickets("octocat@example.com", "PROJ")
        assert len(tickets) == 1
        assert (
            "https://github.com/exampleorg/widgets/issues/1" in tickets[0].remote_links
        )
        assert "https://github.com/exampleorg/widgets/pull/2" in tickets[0].remote_links
        jql = client._request.call_args_list[0].kwargs["params"]["jql"]
        assert 'project = "PROJ"' in jql


class TestFindTrackingTicket:
    def test_primary_remote_link_hit_returns_ticket(self):
        client = _client()
        client._request.return_value = _search_hit("PROJ-42")

        result = client.find_tracking_ticket(
            "https://github.com/exampleorg/widgets/issues/1", "PROJ"
        )

        assert result is not None and result.key == "PROJ-42"
        assert client._request.call_count == 1
        jql = client._request.call_args.kwargs["params"]["jql"]
        assert "issuesWithRemoteLinksByGlobalId" in jql

    def test_fallback_hit_backfills_remote_link(self):
        client = _client()
        client._request.side_effect = [
            _resp({"issues": []}),
            _search_hit("PROJ-99"),
            _resp({}),
        ]

        result = client.find_tracking_ticket(
            "https://github.com/exampleorg/widgets/issues/1", "PROJ"
        )

        assert result is not None and result.key == "PROJ-99"
        backfill = client._request.call_args_list[2]
        assert backfill.args[0] == "POST"
        assert backfill.args[1].endswith("/remotelink")
        assert backfill.kwargs["json"]["relationship"] == "GitHub issue"

    def test_both_miss_returns_none(self):
        client = _client()
        client._request.side_effect = [_resp({"issues": []}), _resp({"issues": []})]
        assert (
            client.find_tracking_ticket(
                "https://github.com/exampleorg/widgets/issues/1", "PROJ"
            )
            is None
        )

    def test_transient_jql_error_returns_none(self):
        client = _client()
        client._request.side_effect = requests.RequestException("503")
        assert (
            client.find_tracking_ticket(
                "https://github.com/exampleorg/widgets/pull/1", "PROJ"
            )
            is None
        )


class TestSprints:
    def test_get_sprint_by_name_found(self):
        client = _client()
        client._request.return_value = _resp(
            {
                "isLast": True,
                "values": [
                    {"id": 1, "name": "Sprint 34", "state": "closed"},
                    {"id": 68993, "name": "Sprint 35", "state": "active"},
                ],
            }
        )
        assert client.get_sprint_by_name(42, "Sprint 35") == SprintRef(
            68993, "Sprint 35"
        )

    def test_get_sprint_by_name_absent(self):
        client = _client()
        client._request.return_value = _resp(
            {
                "isLast": True,
                "values": [{"id": 1, "name": "Sprint 34", "state": "closed"}],
            }
        )
        assert client.get_sprint_by_name(42, "Sprint 35") is None


class TestDryRunJiraClient:
    def _dry(self) -> DryRunJiraClient:
        client = DryRunJiraClient.__new__(DryRunJiraClient)
        client._session = MagicMock()
        client._request = MagicMock()
        return client

    def test_post_comment_does_not_call_request(self):
        client = self._dry()
        client.post_comment(make_ticket("PROJ-100", "Test"), make_pr(), "In Review")
        client._request.assert_not_called()

    def test_transition_logs_without_calling_api(self):
        client = self._dry()
        assert (
            client.transition_ticket(
                make_ticket("PROJ-100", "Test", status="In Progress"), "In Review"
            )
            is True
        )
        assert (
            client.transition_ticket(
                make_ticket("PROJ-100", "Test", status="In Review"), "In Review"
            )
            is False
        )
        client._request.assert_not_called()

    def test_create_ticket_accepts_kwargs_and_reflects_initial_status(self):
        client = self._dry()
        ticket = client.create_ticket(
            "PROJ",
            "Test summary",
            {"type": "doc", "version": 1, "content": []},
            "octocat@example.com",
            labels=["rfc"],
            extra_fields={"customfield_12345": 3.0},
            issuetype="Initiative",
            parent_key="PROJ-1",
            initial_status_name="New",
        )
        assert ticket.key == "PROJ-DRYRUN"
        assert ticket.status == "New"
        client._request.assert_not_called()

    def test_label_and_team_writes_are_noops(self):
        client = self._dry()
        client.update_labels("PROJ-100", ["sprint-2026-06"])
        client.set_team("PROJ-100", "team-uuid-alpha", "customfield_11111")
        client.set_parent("PROJ-100", "PROJ-9")
        client._request.assert_not_called()

"""GitHubClient GraphQL/REST parsing and the shared HTTP layer."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import make_graphql_pr_node
from upstream_jira_sync.github import GitHubClient, _parse_closing_refs
from upstream_jira_sync.http import BaseHTTPClient
from upstream_jira_sync.models import ReviewDecision


def _client(**kwargs) -> GitHubClient:
    return GitHubClient("tok", ["exampleorg/widgets"], **kwargs)


def _graphql_resp(payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _search_page(nodes) -> dict:
    return {
        "data": {
            "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }
        }
    }


class TestBaseHTTPClient:
    def test_request_sets_default_timeout(self):
        client = BaseHTTPClient()
        client._session = MagicMock()
        client._session.request.return_value = _graphql_resp({})

        client._request("GET", "https://example.com")

        _, kwargs = client._session.request.call_args
        assert kwargs["timeout"] == (10, 30)

    def test_retries_on_429(self):
        client = BaseHTTPClient()
        rate_resp = MagicMock(status_code=429, headers={"Retry-After": "0"})
        ok_resp = _graphql_resp({})
        client._session = MagicMock()
        client._session.request.side_effect = [rate_resp, ok_resp]

        with patch("upstream_jira_sync.http.time.sleep"):
            result = client._request("GET", "https://example.com")

        assert result == ok_resp
        assert client._session.request.call_count == 2

    def test_does_not_retry_403_without_retry_after(self):
        client = BaseHTTPClient()
        forbidden = MagicMock(status_code=403, headers={})
        forbidden.url = "https://example.com/api"
        client._session = MagicMock()
        client._session.request.return_value = forbidden

        with pytest.raises(requests.HTTPError, match="403 error"):
            client._request("GET", "https://example.com")
        assert client._session.request.call_count == 1

    def test_raises_after_max_retries(self):
        client = BaseHTTPClient()
        client._session = MagicMock()
        client._session.request.return_value = MagicMock(
            status_code=429, headers={"Retry-After": "0"}
        )

        with patch("upstream_jira_sync.http.time.sleep"):
            with pytest.raises(RuntimeError, match="Exceeded 4 retries"):
                client._request("GET", "https://example.com")

    def test_context_manager_closes_session(self):
        client = BaseHTTPClient()
        client._session = MagicMock()
        with client:
            pass
        client._session.close.assert_called_once()


class TestParsePRNode:
    def test_last_human_activity_ignores_bot_comments(self):
        node = make_graphql_pr_node(state="OPEN")
        node["createdAt"] = "2026-03-01T10:00:00Z"
        node["comments"] = {
            "nodes": [
                {
                    "createdAt": "2026-03-10T10:00:00Z",
                    "author": {"login": "contributor", "__typename": "User"},
                },
                {
                    "createdAt": "2026-03-19T10:00:00Z",
                    "author": {"login": "merge-bot", "__typename": "User"},
                },
                {
                    "createdAt": "2026-03-20T09:00:00Z",
                    "author": {"login": "github-actions[bot]", "__typename": "Bot"},
                },
            ]
        }
        client = _client(ignore_activity_authors=["merge-bot"])
        pr, _, _ = client._parse_pr_node(node)
        assert pr.last_human_activity_at == "2026-03-10T10:00:00Z"

    def test_last_human_activity_counts_ready_for_review(self):
        # Marking a draft ready counts as human activity; a bot doing so does not.
        node = make_graphql_pr_node(state="OPEN")
        node["createdAt"] = "2026-03-01T10:00:00Z"
        node["timelineItems"] = {
            "nodes": [
                {
                    "createdAt": "2026-03-05T10:00:00Z",
                    "actor": {"login": "contributor", "__typename": "User"},
                },
                {
                    "createdAt": "2026-03-09T10:00:00Z",
                    "actor": {"login": "merge-bot", "__typename": "User"},
                },
            ]
        }
        client = _client(ignore_activity_authors=["merge-bot"])
        pr, _, _ = client._parse_pr_node(node)
        assert pr.last_human_activity_at == "2026-03-05T10:00:00Z"

    def test_commit_authors_keeps_humans_only(self):
        # PR author stays in commit_authors: author-exclusion is the orchestrator's job.
        node = make_graphql_pr_node(
            commit_authors=["octocat", "coauthor", None, "claude[bot]", "merge-bot"]
        )
        client = _client(ignore_activity_authors=["merge-bot"])
        pr, _, _ = client._parse_pr_node(node)
        assert pr.commit_authors == ("octocat", "coauthor")

    def test_open_and_merged_states(self):
        client = _client()
        pr, _, _ = client._parse_pr_node(make_graphql_pr_node(state="OPEN"))
        assert pr.state == "open" and pr.merged is False

        pr, _, _ = client._parse_pr_node(
            make_graphql_pr_node(state="MERGED", merged=True)
        )
        assert pr.state == "closed" and pr.merged is True

    def test_merge_labels_stamped_from_config(self):
        node = make_graphql_pr_node(state="CLOSED", labels=["Merged"])
        pr, _, _ = _client(merge_labels=("Merged",))._parse_pr_node(node)
        assert pr.merge_labels == ("Merged",)
        assert pr.effectively_merged is True

        pr_default, _, _ = _client()._parse_pr_node(node)
        assert pr_default.merge_labels == ()
        assert pr_default.effectively_merged is False

    def test_changes_requested_counts_reviewers(self):
        node = make_graphql_pr_node(
            state="OPEN",
            review_decision="CHANGES_REQUESTED",
            reviews=[
                {"author": {"login": "r1"}, "state": "CHANGES_REQUESTED"},
                {"author": {"login": "r2"}, "state": "CHANGES_REQUESTED"},
                {"author": {"login": "r3"}, "state": "APPROVED"},
            ],
        )
        _, decision, count = _client()._parse_pr_node(node)
        assert decision == ReviewDecision.CHANGES_REQUESTED
        assert count == 2

    def test_commented_and_dismissed_reviews_filtered(self):
        node = make_graphql_pr_node(
            state="OPEN",
            review_decision="CHANGES_REQUESTED",
            reviews=[
                {"author": {"login": "r1"}, "state": "CHANGES_REQUESTED"},
                {"author": {"login": "r2"}, "state": "COMMENTED"},
                {"author": {"login": "r3"}, "state": "DISMISSED"},
            ],
        )
        _, _, count = _client()._parse_pr_node(node)
        assert count == 1

    def test_body_truncated_to_600_chars(self):
        node = make_graphql_pr_node()
        node["body"] = "x" * 1000
        pr, _, _ = _client()._parse_pr_node(node)
        assert len(pr.body) == 600


class TestGraphQLTransport:
    def test_null_data_raises(self):
        client = _client()
        client._session = MagicMock()
        client._session.request.return_value = _graphql_resp({"data": None})
        with pytest.raises(RuntimeError, match="null data"):
            client._graphql("query {}", {})

    def test_errors_raises(self):
        client = _client()
        client._session = MagicMock()
        client._session.request.return_value = _graphql_resp(
            {"errors": [{"message": "bad query"}]}
        )
        with pytest.raises(RuntimeError, match="GraphQL errors"):
            client._graphql("query {}", {})

    def test_base_url_reroutes_graphql_and_rest(self):
        client = _client(base_url="http://localhost:9999")
        assert client._graphql_url == "http://localhost:9999/graphql"

        client._session = MagicMock()
        resp = _graphql_resp({})
        resp.text = "diff --git a/f.py b/f.py\n"
        client._session.request.return_value = resp
        client.get_pr_diff("exampleorg/widgets", 5)
        url = client._session.request.call_args.args[1]
        assert url == "http://localhost:9999/repos/exampleorg/widgets/pulls/5"


class TestSearchQueries:
    def test_get_prs_by_user_query(self):
        client = _client()
        client._graphql = MagicMock(
            return_value={
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                }
            }
        )
        client.get_prs_by_user("octocat", "2026-03-01T00:00:00")
        _, variables = client._graphql.call_args.args
        assert "author:octocat" in variables["query"]
        assert "updated:>=2026-03-01T00:00:00" in variables["query"]

    def test_get_prs_by_filter_builds_full_query(self):
        client = _client()
        client._graphql = MagicMock(
            return_value={
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                }
            }
        )
        client.get_prs_by_filter(
            authors=["octocat", "hubot"],
            labels=["bug", "module: dynamo"],
            created_after=datetime(2026, 3, 1),
            created_before=datetime(2026, 3, 15),
            is_open=True,
            is_draft=False,
            is_merged=False,
        )
        _, variables = client._graphql.call_args.args
        query = variables["query"]
        assert "repo:exampleorg/widgets" in query
        assert "author:octocat" in query and "author:hubot" in query
        assert "label:bug" in query and 'label:"module: dynamo"' in query
        assert "is:open" in query
        assert "draft:false" in query
        assert "is:unmerged" in query
        assert "created:2026-03-01..2026-03-15" in query

    def test_get_prs_by_filter_defaults_leave_dimensions_unfiltered(self):
        client = _client()
        client._graphql = MagicMock(
            return_value={
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                }
            }
        )
        client.get_prs_by_filter(created_after=datetime(2026, 3, 1, 12, 30))
        _, variables = client._graphql.call_args.args
        query = variables["query"]
        assert "is:pr" in query
        assert "created:>=2026-03-01T12:30:00" in query
        for absent in ("author:", "label:", "is:open", "draft:", "is:merged"):
            assert absent not in query

    def test_get_pr_parses_single_pull_request(self):
        client = _client()
        client._graphql = MagicMock(
            return_value={
                "repository": {
                    "pullRequest": {
                        "number": 9,
                        "title": "Fix transport",
                        "url": "https://github.com/exampleorg/widgets/pull/9",
                        "state": "CLOSED",
                        "merged": False,
                        "isDraft": False,
                        "updatedAt": "2026-03-20T10:00:00Z",
                    }
                }
            }
        )
        pr = client.get_pr("exampleorg/widgets", 9)
        assert pr is not None
        assert pr.number == 9
        assert pr.state == "closed" and pr.merged is False


class TestCompetingPR:
    def _competing_client(self, nodes) -> GitHubClient:
        client = _client()
        client._graphql = MagicMock(
            return_value={"repository": {"issue": {"timelineItems": {"nodes": nodes}}}}
        )
        return client

    def test_competing_pr_by_other_author(self):
        client = self._competing_client(
            [
                {
                    "willCloseTarget": True,
                    "source": {
                        "__typename": "PullRequest",
                        "state": "OPEN",
                        "author": {"login": "hubot"},
                    },
                }
            ]
        )
        assert client.issue_has_competing_pr("exampleorg/widgets", 88, "octocat")

    def test_claimers_own_pr_is_not_competing(self):
        client = self._competing_client(
            [
                {
                    "willCloseTarget": True,
                    "source": {
                        "__typename": "PullRequest",
                        "state": "OPEN",
                        "author": {"login": "octocat"},
                    },
                }
            ]
        )
        assert not client.issue_has_competing_pr("exampleorg/widgets", 88, "octocat")

    def test_mention_and_abandoned_prs_are_not_competing(self):
        client = self._competing_client(
            [
                {
                    "willCloseTarget": False,
                    "source": {
                        "__typename": "PullRequest",
                        "state": "OPEN",
                        "author": {"login": "someone"},
                    },
                },
                {
                    "willCloseTarget": True,
                    "source": {
                        "__typename": "PullRequest",
                        "state": "CLOSED",
                        "author": {"login": "other"},
                    },
                },
            ]
        )
        assert not client.issue_has_competing_pr("exampleorg/widgets", 88, "octocat")


class TestClosingKeywordRegex:
    @pytest.mark.parametrize(
        "body,expected",
        [
            ("Fixes issue #2", [2]),
            ("Closes: #5", [5]),
            ("fixed #1 and closed #2 and resolved #3", [1, 2, 3]),
            ("addresses #42 and related to #99", []),
            ("<!-- Fixes #1 -->\nreal content with Fixes #2", [2]),
            ("```\nexample: Fixes #1\n```\nFixes #2", [2]),
            ("> Fixes #1 (quoted from template)\nFixes #2", [2]),
        ],
    )
    def test_parse_closing_refs(self, body, expected):
        assert _parse_closing_refs(body) == expected


class TestPRBodyRegexIntegration:
    def _node(self, body: str, closing_refs=None) -> dict:
        return {
            "number": 3,
            "title": "PR title",
            "url": "https://github.com/exampleorg/widgets/pull/3",
            "state": "OPEN",
            "isDraft": False,
            "merged": False,
            "updatedAt": "2026-04-01T00:00:00Z",
            "body": body,
            "author": {"login": "octocat"},
            "reviewDecision": None,
            "reviews": {"nodes": []},
            "closingIssuesReferences": {"nodes": closing_refs or []},
        }

    def test_regex_found_issue_gets_fetched_and_merged(self):
        client = _client()
        client._session = MagicMock()
        client._session.request.return_value = _graphql_resp(
            {
                "number": 2,
                "title": "Test Issue",
                "html_url": "https://github.com/exampleorg/widgets/issues/2",
                "body": "issue body",
            }
        )

        pr, _, _ = client._parse_pr_node(self._node("Fixes issue #2"))
        assert len(pr.linked_issues) == 1
        assert pr.linked_issues[0].number == 2
        assert (
            pr.linked_issues[0].url == "https://github.com/exampleorg/widgets/issues/2"
        )

    def test_graphql_match_not_duplicated_by_regex(self):
        client = _client()
        client._session = MagicMock()
        refs = [
            {
                "number": 2,
                "title": "Issue 2",
                "url": "https://github.com/exampleorg/widgets/issues/2",
                "body": "b",
            }
        ]
        pr, _, _ = client._parse_pr_node(self._node("Fixes #2", closing_refs=refs))
        assert len(pr.linked_issues) == 1
        client._session.request.assert_not_called()

    def test_issue_fetch_failure_skips_gracefully(self):
        client = _client()
        client._session = MagicMock()
        client._session.request.side_effect = requests.RequestException("404")
        pr, _, _ = client._parse_pr_node(self._node("Fixes issue #999"))
        assert pr.linked_issues == ()


class TestGetIssues:
    def test_query_construction(self):
        client = _client()
        client._session = MagicMock()
        client._session.request.return_value = _graphql_resp(_search_page([]))

        client.get_issues(
            labels=["bug", "triaged"],
            is_open=True,
            has_linked_pr=True,
            is_available=True,
            comment_only=False,
        )

        query = client._session.request.call_args.kwargs["json"]["variables"]["query"]
        assert "repo:exampleorg/widgets" in query
        assert "is:issue" in query
        assert "is:open" in query
        assert 'label:"bug"' in query and 'label:"triaged"' in query
        # is_available forces has_linked_pr to False
        assert "-linked:pr" in query

    def _issue_node(self, timeline_nodes) -> dict:
        return {
            "number": 12345,
            "title": "Bug: something broken",
            "url": "https://github.com/exampleorg/widgets/issues/12345",
            "state": "OPEN",
            "body": "Issue description",
            "comments": {"nodes": []},
            "timelineItems": {"nodes": timeline_nodes},
        }

    def test_referenced_issues_filtered_when_available_only(self):
        commit_ref = {
            "__typename": "ReferencedEvent",
            "createdAt": "2026-01-15T10:30:00Z",
            "isCrossRepository": False,
            "commit": {"oid": "abc123"},
        }
        pr_ref = {
            "__typename": "CrossReferencedEvent",
            "createdAt": "2026-01-15T10:30:00Z",
            "isCrossRepository": False,
            "source": {"__typename": "PullRequest", "number": 54321},
        }
        for ref in (commit_ref, pr_ref):
            client = _client()
            client._session = MagicMock()
            client._session.request.return_value = _graphql_resp(
                _search_page([self._issue_node([ref])])
            )

            assert len(client.get_issues(is_open=True, comment_only=False)) == 1
            assert (
                client.get_issues(is_open=True, is_available=True, comment_only=False)
                == []
            )


class TestReviewActivity:
    def test_parses_reviews_and_normalizes_timestamps(self):
        client = _client()
        client._graphql = MagicMock(
            return_value={
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 1,
                            "url": "https://github.com/exampleorg/widgets/pull/1",
                            "author": {"login": "hubot"},
                            "reviews": {
                                "nodes": [
                                    {
                                        "state": "APPROVED",
                                        "submittedAt": "2026-06-02T08:00:00Z",
                                        "comments": {"totalCount": 3},
                                    }
                                ]
                            },
                        },
                        {
                            "number": 2,
                            "url": "https://github.com/exampleorg/widgets/pull/2",
                            "author": None,
                            "reviews": {"nodes": []},
                        },
                    ],
                }
            }
        )

        results = client.get_review_activity("octocat", "2026-06-01")

        _, variables = client._graphql.call_args.args
        assert "reviewed-by:octocat" in variables["query"]
        assert "-author:octocat" in variables["query"]
        assert results == [
            {
                "pr_url": "https://github.com/exampleorg/widgets/pull/1",
                "pr_author": "hubot",
                "reviews": [
                    {
                        "state": "APPROVED",
                        "submitted_at": "2026-06-02T08:00:00+00:00",
                        "comment_count": 3,
                    }
                ],
            },
            {
                "pr_url": "https://github.com/exampleorg/widgets/pull/2",
                "pr_author": "",
                "reviews": [],
            },
        ]


class TestGetPRDiff:
    def test_requests_with_diff_accept_header(self):
        client = _client()
        client._session = MagicMock()
        resp = _graphql_resp({})
        resp.text = "diff --git a/file.py b/file.py\n..."
        client._session.request.return_value = resp

        diff = client.get_pr_diff("exampleorg/widgets", 12345)

        assert diff == "diff --git a/file.py b/file.py\n..."
        call_args = client._session.request.call_args
        assert call_args.args[0] == "GET"
        assert (
            call_args.args[1]
            == "https://api.github.com/repos/exampleorg/widgets/pulls/12345"
        )
        assert call_args.kwargs["headers"]["Accept"] == "application/vnd.github.v3.diff"

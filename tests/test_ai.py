"""AI classes, SkillLoader, teams helpers, and LLM providers."""

import os
from unittest.mock import MagicMock, patch

import pytest
import requests
from conftest import (
    FakeLLM,
    make_issue,
    make_linked_issue,
    make_pr,
    make_teams,
    make_ticket,
)
from upstream_jira_sync.ai import (
    MATCH_SHORTLIST_SIZE,
    AITicketMatcher,
    IssueClaimClassifier,
    IssueDeduplicator,
    IssueSummarizer,
    RfcClassifier,
    StoryPointEstimator,
    TeamClassifier,
)
from upstream_jira_sync.config import LLMSettings
from upstream_jira_sync.http import RetryExhaustedError
from upstream_jira_sync.llm.anthropic import AnthropicProvider
from upstream_jira_sync.llm.base import (
    LLMError,
    LLMFatalError,
    load_provider,
    reasoning_params,
)
from upstream_jira_sync.llm.vertex import VertexProvider
from upstream_jira_sync.models import LinkedIssue
from upstream_jira_sync.skill_loader import SkillLoader, is_bot_actor, is_bot_author
from upstream_jira_sync.teams import (
    primary_team_id,
    render_team_prompt_section,
    teams_to_labels,
)

_LOADER = SkillLoader()


class TestSkillLoader:
    def test_packaged_default_loads(self):
        content = SkillLoader().load("ticket_matcher")
        assert content
        assert not content.startswith("---")

    def test_override_dir_wins_and_frontmatter_stripped(self, tmp_path):
        (tmp_path / "ticket_matcher.md").write_text(
            "---\nname: Test\ndescription: A test\n---\n\nOverride content here."
        )
        loader = SkillLoader(override_dir=str(tmp_path))
        assert loader.load("ticket_matcher") == "Override content here."

    def test_missing_skill_raises(self, tmp_path):
        loader = SkillLoader(override_dir=str(tmp_path))
        with pytest.raises(FileNotFoundError, match="nonexistent"):
            loader.load("nonexistent")

    def test_override_missing_template_vars_fails_fast(self, tmp_path):
        (tmp_path / "team_classification.md").write_text(
            "Classify\nTitle: {pr_title}\nBody: {pr_body}\n"
        )
        loader = SkillLoader(override_dir=str(tmp_path))
        with pytest.raises(ValueError) as exc_info:
            loader.load("team_classification")
        msg = str(exc_info.value)
        assert "team_classification" in msg
        assert "file_paths" in msg and "teams_section" in msg

    def test_override_with_all_template_vars_passes(self, tmp_path):
        (tmp_path / "team_classification.md").write_text(
            "Teams:\n{teams_section}\nTitle: {pr_title}\nBody: {pr_body}\nFiles: {file_paths}\n"
        )
        loader = SkillLoader(override_dir=str(tmp_path))
        assert "{teams_section}" in loader.load("team_classification")


class TestBotIdentity:
    def test_is_bot_author(self):
        assert is_bot_author("  Bot@Example.COM ", "bot@example.com") is True
        assert is_bot_author("human@example.com", "bot@example.com") is False
        assert is_bot_author(None, "bot@example.com") is False
        assert is_bot_author("bot@example.com", "") is False

    def test_is_bot_actor_matches_email_accountid_and_aliases(self):
        bot_email, bot_account = "bot@example.com", "acct-bot"
        aliases = ("old-bot@example.com", "acct-old")
        assert is_bot_actor({"emailAddress": "Bot@Example.COM"}, bot_email, bot_account)
        assert is_bot_actor({"accountId": "acct-bot"}, bot_email, bot_account)
        assert is_bot_actor(
            {"emailAddress": "old-bot@example.com"}, bot_email, bot_account, aliases
        )
        assert is_bot_actor({"accountId": "acct-old"}, bot_email, bot_account, aliases)
        assert not is_bot_actor(
            {"emailAddress": "human@example.com", "accountId": "999"},
            bot_email,
            bot_account,
        )

    def test_is_bot_actor_empty_author_is_not_bot(self):
        assert is_bot_actor(None, "bot@example.com", "acct-1") is False
        assert is_bot_actor({}, "bot@example.com", "acct-1") is False


def make_matcher(response_text='{"key": null, "confidence": "low", "reason": "none"}'):
    llm = FakeLLM(response_text)
    return AITicketMatcher(llm=llm, skill_loader=_LOADER), llm


class TestAITicketMatcher:
    def test_empty_tickets_returns_none(self):
        matcher, _ = make_matcher()
        assert matcher.find_best(make_pr(), []) is None

    def test_non_high_confidence_returns_none(self):
        matcher, _ = make_matcher(
            '{"key": "PROJ-100", "confidence": "medium", "reason": "maybe"}'
        )
        assert (
            matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")]) is None
        )

    def test_high_confidence_returns_match_result(self):
        matcher, _ = make_matcher(
            '{"key": "PROJ-100", "confidence": "high", "reason": "Clear match"}'
        )
        result = matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")])
        assert result is not None
        assert result.ticket.key == "PROJ-100"
        assert result.confidence == "high"
        assert result.reason == "Clear match"

    def test_unknown_key_returns_none(self):
        matcher, _ = make_matcher(
            '{"key": "PROJ-999", "confidence": "high", "reason": "Match"}'
        )
        assert (
            matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")]) is None
        )

    def test_markdown_fences_stripped(self):
        matcher, _ = make_matcher(
            '```json\n{"key": "PROJ-100", "confidence": "high", "reason": "match"}\n```'
        )
        result = matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")])
        assert result is not None and result.ticket.key == "PROJ-100"

    def test_api_failure_returns_none(self):
        matcher = AITicketMatcher(
            llm=FakeLLM(error=Exception("network error")), skill_loader=_LOADER
        )
        assert (
            matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")]) is None
        )

    def test_url_match_returns_high_without_calling_llm(self):
        matcher, llm = make_matcher(
            '{"key": null, "confidence": "low", "reason": "no"}'
        )
        ticket = make_ticket("PROJ-1", "Unrelated title")
        ticket.remote_links = ["https://github.com/exampleorg/widgets/issues/7"]
        pr = make_pr(
            linked_issues=(
                LinkedIssue(
                    number=7,
                    title="Issue 7",
                    url="https://github.com/exampleorg/widgets/issues/7",
                ),
            )
        )

        result = matcher.find_best(pr, [ticket])

        assert result is not None
        assert result.ticket.key == "PROJ-1"
        assert result.confidence == "high"
        assert llm.calls == []

    def test_no_url_overlap_falls_back_to_llm(self):
        matcher, llm = make_matcher(
            '{"key": "PROJ-1", "confidence": "medium", "reason": "fallback"}'
        )
        ticket = make_ticket("PROJ-1", "Something")
        ticket.remote_links = ["https://github.com/exampleorg/widgets/issues/99"]
        pr = make_pr(
            linked_issues=(
                LinkedIssue(
                    number=7,
                    title="Issue 7",
                    url="https://github.com/exampleorg/widgets/issues/7",
                ),
            )
        )

        assert matcher.find_best(pr, [ticket]) is None
        assert len(llm.calls) == 1

    def test_prompt_includes_description_and_links(self):
        matcher, llm = make_matcher()
        ticket = make_ticket("PROJ-1", "Lowering work")
        ticket.description = "Lower view/reshape ops to fused kernels."
        ticket.remote_links = ["https://github.com/exampleorg/widgets/issues/99"]
        pr = make_pr(
            linked_issues=(
                LinkedIssue(
                    number=11,
                    title="t",
                    url="https://github.com/exampleorg/widgets/issues/11",
                ),
            )
        )

        matcher.find_best(pr, [ticket])

        prompt = llm.calls[0]["user_message"]
        assert "Lower view/reshape ops to fused kernels." in prompt
        assert "https://github.com/exampleorg/widgets/issues/11" in prompt
        assert "https://github.com/exampleorg/widgets/issues/99" in prompt

    # -- item 1: Jira key named in the PR ---------------------------------

    def test_key_in_title_matches_without_calling_llm(self):
        matcher, llm = make_matcher()
        tickets = [make_ticket("PROJ-7", "Other"), make_ticket("PROJ-42", "Retry")]
        pr = make_pr(title="[PROJ-42] Fix transport layer reconnect")

        result = matcher.find_best(pr, tickets)

        assert result is not None
        assert result.ticket.key == "PROJ-42"
        assert result.confidence == "high"
        assert "PROJ-42" in result.reason
        assert "title" in result.reason
        assert llm.calls == []

    def test_key_in_body_matches_without_calling_llm(self):
        matcher, llm = make_matcher()
        tickets = [make_ticket("PROJ-7", "Other"), make_ticket("PROJ-42", "Retry")]
        pr = make_pr(body="Implements the retry loop.\n\nJira: PROJ-42")

        result = matcher.find_best(pr, tickets)

        assert result is not None
        assert result.ticket.key == "PROJ-42"
        assert "description" in result.reason
        assert llm.calls == []

    def test_key_not_in_candidate_pool_falls_through_to_llm(self):
        matcher, llm = make_matcher(
            response_text='{"key": "PROJ-7", "confidence": "high", "reason": "r"}'
        )
        tickets = [make_ticket("PROJ-7", "Other")]
        pr = make_pr(title="OTHER-999 unrelated project key")

        result = matcher.find_best(pr, tickets)

        assert result is not None
        assert result.ticket.key == "PROJ-7"
        assert len(llm.calls) == 1

    def test_lowercase_or_partial_keys_do_not_match(self):
        matcher, llm = make_matcher()
        tickets = [make_ticket("PROJ-42", "Retry")]
        pr = make_pr(title="proj-42 lowercase", body="See XPROJ-42 and PROJ-421.")

        assert matcher.find_best(pr, tickets) is None
        assert len(llm.calls) == 1

    def test_url_match_wins_over_key_match(self):
        matcher, llm = make_matcher()
        url_ticket = make_ticket("PROJ-1", "Linked")
        url_ticket.remote_links = ["https://github.com/exampleorg/widgets/issues/11"]
        key_ticket = make_ticket("PROJ-2", "Named")
        pr = make_pr(
            title="PROJ-2 fix",
            linked_issues=(make_linked_issue(11),),
        )

        result = matcher.find_best(pr, [key_ticket, url_ticket])

        assert result is not None
        assert result.ticket.key == "PROJ-1"
        assert llm.calls == []

    # -- item 2: lexical shortlist ------------------------------------------

    def _pool(self, n, target_index):
        tickets = []
        for i in range(n):
            t = make_ticket(f"PROJ-{i + 1}", f"Backlog item number {i + 1}")
            t.description = "Placeholder scope for an unrelated backlog card."
            tickets.append(t)
        target = tickets[target_index]
        target.summary = "Transport client reconnect retries"
        target.description = "Add a bounded retry loop to the transport client."
        return tickets, target

    def test_small_pool_is_sent_whole(self):
        matcher, llm = make_matcher()
        tickets, _ = self._pool(MATCH_SHORTLIST_SIZE, 3)

        matcher.find_best(make_pr(), tickets)

        prompt = llm.calls[0]["user_message"]
        assert all(t.key + ":" in prompt for t in tickets)

    def test_large_pool_is_trimmed_and_keeps_lexical_match(self):
        matcher, llm = make_matcher()
        tickets, target = self._pool(50, 47)

        matcher.find_best(make_pr(), tickets)

        prompt = llm.calls[0]["user_message"]
        listed = [t for t in tickets if f"{t.key}:" in prompt]
        assert len(listed) == MATCH_SHORTLIST_SIZE
        assert target in listed
        assert f"1. {target.key}:" in prompt

    def test_shortlist_ties_keep_recency_order(self):
        matcher, llm = make_matcher()
        tickets = []
        for i in range(30):
            t = make_ticket(f"PROJ-{i + 1}", "Unrelated card")
            tickets.append(t)

        matcher.find_best(make_pr(), tickets)

        prompt = llm.calls[0]["user_message"]
        listed = [t.key for t in tickets if f"{t.key}:" in prompt]
        assert listed == [f"PROJ-{i + 1}" for i in range(MATCH_SHORTLIST_SIZE)]

    def test_model_key_outside_shortlist_is_rejected(self):
        tickets, _ = self._pool(50, 0)
        dropped = tickets[-1]  # zero overlap, last in recency order
        matcher, llm = make_matcher(
            response_text=(
                '{"key": "%s", "confidence": "high", "reason": "r"}' % dropped.key
            )
        )

        assert matcher.find_best(make_pr(), tickets) is None

    def test_non_high_warning_reports_shortlist_size(self, caplog):
        matcher, _ = make_matcher()
        tickets, _ = self._pool(50, 0)

        with caplog.at_level("WARNING"):
            matcher.find_best(make_pr(), tickets)

        assert f"from {MATCH_SHORTLIST_SIZE} candidates" in caplog.text


class TestStoryPointEstimator:
    def _estimator(self, response_text):
        return StoryPointEstimator(llm=FakeLLM(response_text), skill_loader=_LOADER)

    def test_valid_estimate(self):
        est = self._estimator('{"points": 5, "reason": "Medium"}')
        assert est.estimate(make_pr(), make_ticket("PROJ-1", "Test")) == 5

    def test_invalid_points_returns_none(self):
        est = self._estimator('{"points": 7, "reason": "bad"}')
        assert est.estimate(make_pr(), make_ticket("PROJ-1", "Test")) is None

    def test_api_failure_returns_none(self):
        est = StoryPointEstimator(
            llm=FakeLLM(error=Exception("down")), skill_loader=_LOADER
        )
        assert est.estimate(make_pr(), make_ticket("PROJ-1", "Test")) is None

    def test_estimate_from_issue(self):
        est = self._estimator('{"points": 3, "reason": "medium"}')
        assert (
            est.estimate_from_issue(make_ticket("PROJ-1", "x"), "Fix kernel", "details")
            == 3
        )

        bad = self._estimator('{"points": 4, "reason": "x"}')
        assert bad.estimate_from_issue(make_ticket("PROJ-1", "x"), "t", "b") is None


class TestIssueClaimClassifier:
    def _classifier(self, response_text="", error=None):
        return IssueClaimClassifier(
            llm=FakeLLM(response_text, error=error), skill_loader=_LOADER
        )

    def test_claiming_intent(self):
        clf = self._classifier(
            '{"intent": "claiming", "reason": "User will submit a fix"}'
        )
        assert (
            clf.classify(make_issue(), "I'll submit a fix", "octocat").intent
            == "claiming"
        )

    def test_invalid_intent_defaults_to_not_claiming(self):
        clf = self._classifier('{"intent": "maybe", "reason": "unclear"}')
        assert clf.classify(make_issue(), "comment", "octocat").intent == "not_claiming"

    def test_api_failure_returns_not_claiming(self):
        clf = self._classifier(error=Exception("API down"))
        assert (
            clf.classify(make_issue(), "I'll fix this", "octocat").intent
            == "not_claiming"
        )


class TestIssueDeduplicator:
    def test_high_confidence_returns_match(self):
        d = IssueDeduplicator(
            llm=FakeLLM(
                '{"key": "PROJ-1", "confidence": "high", "reason": "same bug"}'
            ),
            skill_loader=_LOADER,
        )
        tickets = [
            make_ticket("PROJ-1", "matrix op rejects dense input"),
            make_ticket("PROJ-2", "x"),
        ]
        result = d.find_existing("matrix op rejects dense input", "body", tickets)
        assert result is not None and result.ticket.key == "PROJ-1"

    def test_medium_confidence_returns_none(self):
        d = IssueDeduplicator(
            llm=FakeLLM('{"key": "PROJ-1", "confidence": "medium", "reason": "maybe"}'),
            skill_loader=_LOADER,
        )
        assert d.find_existing("t", "b", [make_ticket("PROJ-1", "s")]) is None


class TestIssueSummarizer:
    def test_returns_summary_text(self):
        s = IssueSummarizer(
            llm=FakeLLM("Fixes 64-bit overflow in range codegen."), skill_loader=_LOADER
        )
        assert "overflow" in s.summarize("Fix overflow", "Long body about the bug...")

    def test_api_failure_returns_empty(self):
        s = IssueSummarizer(llm=FakeLLM(error=Exception("down")), skill_loader=_LOADER)
        assert s.summarize("title", "body") == ""


class TestRfcClassifier:
    def _classifier(self, response_text="", error=None):
        return RfcClassifier(
            llm=FakeLLM(response_text, error=error), skill_loader=_LOADER
        )

    def test_valid_verdict_passes_through(self):
        clf = self._classifier('{"verdict": "epic", "reason": "scope"}')
        assert clf.classify("[RFC] Foo", "body") == "epic"

    def test_unknown_verdict_returns_none(self):
        clf = self._classifier('{"verdict": "container", "reason": "?"}')
        assert clf.classify("[RFC] Foo", "body") is None

    def test_api_failure_returns_none(self):
        clf = self._classifier(error=Exception("down"))
        assert clf.classify("[RFC] Foo", "body") is None


class TestTeamClassifier:
    def _classifier(
        self, response_text="", error=None
    ) -> tuple[TeamClassifier, FakeLLM]:
        llm = FakeLLM(response_text, error=error)
        return TeamClassifier(
            llm=llm, skill_loader=SkillLoader(), teams=make_teams()
        ), llm

    def test_classify_returns_canonical_teams(self):
        clf, _ = self._classifier('["Team Alpha"]')
        assert clf.classify("t", "b", ("a.py",)) == {"Team Alpha"}

    def test_classify_handles_garbage_response(self):
        clf, _ = self._classifier("Sure, here are the teams: none")
        assert clf.classify("t", "b", ("a.py",)) == set()

    def test_classify_api_error_returns_empty(self):
        clf, _ = self._classifier(error=Exception("API down"))
        assert clf.classify("t", "b", ("a.py",)) == set()

    def test_classify_ordered_keeps_primary_first_and_dedups(self):
        clf, _ = self._classifier(
            '["Team Beta", "Team Alpha", "Team Beta", "Team Delta"]'
        )
        assert clf.classify_ordered("t", "b", ("a.py",)) == ["Team Beta", "Team Alpha"]

    def test_prompt_templated_from_config_teams(self):
        clf, llm = self._classifier("[]")
        clf.classify_ordered("Fix reconnect", "body text", ("net/a.py",))
        prompt = llm.calls[0]["user_message"]
        assert "- Team Alpha" in prompt
        assert "- Team Beta" in prompt
        assert "Fix reconnect" in prompt
        assert "net/a.py" in prompt


class TestTeamsHelpers:
    def test_teams_to_labels_sorted_and_unknown_dropped(self):
        labels = teams_to_labels(
            make_teams(), ["Team Beta", "Team Alpha", "Team Delta"]
        )
        assert labels == ["team-alpha", "team-beta"]

    def test_primary_team_id_first_mapped(self):
        assert (
            primary_team_id(make_teams(), ["Team Beta", "Team Alpha"])
            == "team-uuid-beta"
        )
        assert primary_team_id(make_teams(), []) is None
        # Configured team without a team_id resolves to None.
        assert primary_team_id(make_teams(), ["Team Gamma"]) is None

    def test_render_team_prompt_section(self):
        assert render_team_prompt_section(make_teams()) == (
            "- Team Alpha\n- Team Beta\n- Team Gamma"
        )


def _llm_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"content": [{"text": text}]}
    return resp


class TestAnthropicProvider:
    def test_requires_api_key_unless_mocked(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                AnthropicProvider(LLMSettings(provider="anthropic", model="m"))
            AnthropicProvider(
                LLMSettings(
                    provider="anthropic", model="m", base_url="http://localhost:9999"
                )
            )

    def test_complete_posts_message_and_returns_text(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True):
            provider = AnthropicProvider(
                LLMSettings(provider="anthropic", model="test-model")
            )
        provider._session = MagicMock()
        provider._session.request.return_value = _llm_response("  hello  ")

        result = provider.complete("sys", "user msg", max_tokens=128)

        assert result == "hello"
        call = provider._session.request.call_args
        assert call.args[1] == "https://api.anthropic.com/v1/messages"
        body = call.kwargs["json"]
        assert body["model"] == "test-model"
        assert body["max_tokens"] == 128
        # System prompt is sent as a cacheable block (prompt caching).
        assert body["system"] == [
            {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
        ]
        assert body["messages"] == [{"role": "user", "content": "user msg"}]

    def test_complete_skips_leading_thinking_block(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True):
            provider = AnthropicProvider(LLMSettings(provider="anthropic", model="m"))
        provider._session = MagicMock()
        resp = _llm_response("")
        resp.json.return_value = {
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": '{"teams": '},
                {"type": "text", "text": '["Team Alpha"]}'},
            ],
            "stop_reason": "end_turn",
        }
        provider._session.request.return_value = resp

        assert provider.complete("sys", "user") == '{"teams": ["Team Alpha"]}'

    def test_complete_rejects_body_without_text(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True):
            provider = AnthropicProvider(LLMSettings(provider="anthropic", model="m"))
        provider._session = MagicMock()
        resp = _llm_response("")
        resp.json.return_value = {
            "content": [{"type": "thinking", "thinking": ""}],
            "stop_reason": "max_tokens",
        }
        provider._session.request.return_value = resp

        with pytest.raises(LLMError, match="no text content.*thinking.*max_tokens"):
            provider.complete("sys", "user")

    def test_complete_rejects_truncated_answer(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True):
            provider = AnthropicProvider(LLMSettings(provider="anthropic", model="m"))
        provider._session = MagicMock()
        resp = _llm_response('{"ticket_key": "PROJ-1", "conf')
        resp.json.return_value["stop_reason"] = "max_tokens"
        provider._session.request.return_value = resp

        with pytest.raises(LLMError, match="hit max_tokens=128"):
            provider.complete("sys", "user", max_tokens=128)

    def _body_for(self, model: str, effort: str = "low", thinking: str = "off") -> dict:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True):
            provider = AnthropicProvider(
                LLMSettings(
                    provider="anthropic",
                    model=model,
                    effort=effort,
                    thinking=thinking,
                )
            )
        provider._session = MagicMock()
        provider._session.request.return_value = _llm_response("ok")
        provider.complete("sys", "user", max_tokens=128)
        return provider._session.request.call_args.kwargs["json"]

    def test_thinking_default_model_has_thinking_disabled_by_default(self):
        body = self._body_for("claude-sonnet-5")
        # Sonnet 5 thinks unless told not to; the nightly failures were
        # thinking blocks eating a 128-token backstop before any answer.
        assert body["thinking"] == {"type": "disabled"}
        assert body["output_config"] == {"effort": "low"}
        assert body["max_tokens"] == 128

    def test_thinking_adaptive_adds_headroom(self):
        body = self._body_for("claude-sonnet-5", thinking="adaptive")
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "low"}
        # max_tokens caps thinking + answer when thinking is on, so the
        # caller's short backstop is padded rather than sent verbatim.
        assert body["max_tokens"] == 128 + 1024

    def test_empty_effort_omits_output_config(self):
        body = self._body_for("claude-sonnet-5", effort="")
        assert body["thinking"] == {"type": "disabled"}
        assert "output_config" not in body

    @pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-6"])
    def test_non_thinking_model_gets_no_reasoning_fields(self, model):
        body = self._body_for(model)
        assert "thinking" not in body
        assert "output_config" not in body
        assert body["max_tokens"] == 128

    def test_base_url_reroutes(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = AnthropicProvider(
                LLMSettings(
                    provider="anthropic", model="m", base_url="http://localhost:9999"
                )
            )
        assert provider._url == "http://localhost:9999/v1/messages"


def _api_error(
    status: int, kind: str, message: str, *, key: str = "type"
) -> requests.HTTPError:
    """An HTTPError carrying a JSON error body. ``key`` is ``"type"`` for
    Anthropic-shaped errors and ``"status"`` for Google-shaped Vertex ones."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"error": {key: kind, "message": message}}
    return requests.HTTPError(f"{status} error for url: x", response=resp)


class TestAnthropicProviderErrors:
    def _provider(self) -> AnthropicProvider:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True):
            provider = AnthropicProvider(
                LLMSettings(provider="anthropic", model="claude-sonnet-5")
            )
        provider._session = MagicMock()
        return provider

    @pytest.mark.parametrize(
        "status, err_type, message, hint",
        [
            (401, "authentication_error", "invalid x-api-key", "ANTHROPIC_API_KEY"),
            (403, "permission_error", "no access", "ANTHROPIC_API_KEY"),
            (404, "not_found_error", "model: nope", "llm.model="),
            (400, "invalid_request_error", "Your credit balance is too low", "credit"),
        ],
    )
    def test_fatal_statuses_raise_fatal_with_api_message(
        self, status, err_type, message, hint
    ):
        provider = self._provider()
        provider._session.request.side_effect = _api_error(status, err_type, message)
        with pytest.raises(LLMFatalError) as info:
            provider.complete("sys", "user")
        text = str(info.value)
        assert message in text
        assert str(status) in text
        assert hint in text

    def test_transient_statuses_raise_non_fatal(self):
        provider = self._provider()
        provider._session.request.side_effect = _api_error(
            529, "overloaded_error", "Overloaded"
        )
        with pytest.raises(LLMError) as info:
            provider.complete("sys", "user")
        assert not isinstance(info.value, LLMFatalError)
        assert "Overloaded" in str(info.value)

    def test_non_json_error_body_still_surfaces(self):
        provider = self._provider()
        resp = MagicMock()
        resp.status_code = 502
        resp.json.side_effect = ValueError("no json")
        resp.text = "<html>bad gateway</html>"
        provider._session.request.side_effect = requests.HTTPError(
            "502 error for url: x", response=resp
        )
        with pytest.raises(LLMError, match="bad gateway"):
            provider.complete("sys", "user")

    def test_connection_error_raises_llm_error(self):
        provider = self._provider()
        provider._session.request.side_effect = requests.ConnectionError("refused")
        with pytest.raises(LLMError, match="refused"):
            provider.complete("sys", "user")

    def test_preflight_hits_free_models_endpoint(self):
        provider = self._provider()
        provider._session.request.return_value = MagicMock(status_code=200)
        provider.preflight()
        call = provider._session.request.call_args
        assert call.args[0] == "GET"
        assert call.args[1] == "https://api.anthropic.com/v1/models/claude-sonnet-5"

    def test_preflight_raises_on_bad_key(self):
        provider = self._provider()
        provider._session.request.side_effect = _api_error(
            401, "authentication_error", "invalid x-api-key"
        )
        with pytest.raises(LLMFatalError, match="invalid x-api-key"):
            provider.preflight()

    def test_preflight_raises_on_unknown_model(self):
        provider = self._provider()
        provider._session.request.side_effect = _api_error(
            404, "not_found_error", "model: claude-sonnet-5"
        )
        with pytest.raises(LLMFatalError, match="llm.model="):
            provider.preflight()

    def test_preflight_ignores_inconclusive_failures(self):
        provider = self._provider()
        provider._session.request.side_effect = requests.ConnectionError("refused")
        provider.preflight()  # must not raise


class TestFatalErrorsAreNotSwallowed:
    """Every AI class catches Exception to skip a single bad call; a fatal
    provider error (bad key/model/billing) must propagate instead."""

    def _fatal_llm(self) -> FakeLLM:
        return FakeLLM(error=LLMFatalError("401 authentication_error"))

    def test_ticket_matcher_reraises(self):
        matcher = AITicketMatcher(llm=self._fatal_llm(), skill_loader=_LOADER)
        with pytest.raises(LLMFatalError):
            matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")])

    def test_ticket_matcher_still_skips_transient_errors(self):
        matcher = AITicketMatcher(
            llm=FakeLLM(error=LLMError("529 overloaded")), skill_loader=_LOADER
        )
        assert (
            matcher.find_best(make_pr(), [make_ticket("PROJ-100", "Transport")]) is None
        )

    def test_summarizer_reraises(self):
        with pytest.raises(LLMFatalError):
            IssueSummarizer(llm=self._fatal_llm(), skill_loader=_LOADER).summarize(
                "title", "body"
            )

    def test_story_points_reraises(self):
        est = StoryPointEstimator(llm=self._fatal_llm(), skill_loader=_LOADER)
        with pytest.raises(LLMFatalError):
            est.estimate(make_pr(), make_ticket("PROJ-1", "Test"))


class TestVertexProvider:
    def _provider(self) -> VertexProvider:
        return VertexProvider(
            LLMSettings(
                provider="vertex",
                model="test-model",
                vertex_project="test-project",
                vertex_region="test-region",
                base_url="http://localhost:9999",
            )
        )

    def test_mock_base_url_skips_gcp_auth(self):
        provider = self._provider()
        assert provider._credentials is None
        assert provider._url_prefix.startswith(
            "http://localhost:9999/v1/projects/test-project"
        )

    def test_complete_calls_rawpredict_and_returns_text(self):
        provider = self._provider()
        provider._session = MagicMock()
        provider._session.request.return_value = _llm_response("answer")

        assert provider.complete("sys", "hello") == "answer"
        url = provider._session.request.call_args.args[1]
        assert url.endswith("test-model:rawPredict")
        body = provider._session.request.call_args.kwargs["json"]
        assert body["anthropic_version"] == "vertex-2023-10-16"
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert body["system"][0]["text"] == "sys"

    def test_thinking_default_model_body_has_no_model_key(self):
        provider = VertexProvider(
            LLMSettings(
                provider="vertex",
                model="claude-sonnet-5",
                vertex_project="test-project",
                base_url="http://localhost:9999",
            )
        )
        provider._session = MagicMock()
        provider._session.request.return_value = _llm_response("answer")

        provider.complete("sys", "hello", max_tokens=256)
        body = provider._session.request.call_args.kwargs["json"]
        # Vertex routes by URL; the body carries the reasoning fields only.
        assert "model" not in body
        assert body["thinking"] == {"type": "disabled"}
        assert body["output_config"] == {"effort": "low"}
        assert body["max_tokens"] == 256

    def test_global_region_uses_unprefixed_host(self):
        with patch.object(VertexProvider, "_refresh_token"):
            provider = VertexProvider(
                LLMSettings(
                    provider="vertex",
                    model="claude-sonnet-5",
                    vertex_project="proj",
                    vertex_region="global",
                )
            )
        assert provider._url_prefix.startswith(
            "https://aiplatform.googleapis.com/v1/projects/proj/locations/global/"
        )

    def test_regional_host_keeps_region_prefix(self):
        with patch.object(VertexProvider, "_refresh_token"):
            provider = VertexProvider(
                LLMSettings(
                    provider="vertex",
                    model="m",
                    vertex_project="proj",
                    vertex_region="us-east5",
                )
            )
        assert provider._url_prefix.startswith(
            "https://us-east5-aiplatform.googleapis.com/v1/projects/proj/locations/us-east5/"
        )

    def test_missing_credentials_is_fatal(self):
        from google.auth.exceptions import DefaultCredentialsError

        with patch(
            "upstream_jira_sync.llm.vertex.google.auth.default",
            side_effect=DefaultCredentialsError("no creds"),
        ):
            with pytest.raises(LLMFatalError, match="credentials unavailable"):
                VertexProvider(
                    LLMSettings(
                        provider="vertex",
                        model="m",
                        vertex_project="proj",
                        vertex_region="global",
                    )
                )

    @pytest.mark.parametrize(
        "status,kind,message,expect_fatal",
        [
            (401, "UNAUTHENTICATED", "Request had invalid authentication", True),
            (403, "PERMISSION_DENIED", "Permission denied on resource", True),
            (404, "NOT_FOUND", "Publisher Model not found", True),
            (400, "FAILED_PRECONDITION", "disallowed by Organization Policy", True),
            (429, "RESOURCE_EXHAUSTED", "Quota exceeded for tokens per minute", False),
            (529, "overloaded_error", "Overloaded", False),
            (500, "INTERNAL", "Internal error", False),
        ],
    )
    def test_http_errors_are_classified(self, status, kind, message, expect_fatal):
        provider = self._provider()
        provider._session = MagicMock()
        provider._session.request.side_effect = _api_error(
            status, kind, message, key="status"
        )
        expected = LLMFatalError if expect_fatal else LLMError
        with pytest.raises(expected) as info:
            provider.complete("sys", "hello")
        assert message in str(info.value)
        assert isinstance(info.value, LLMFatalError) is expect_fatal

    def test_retry_exhaustion_is_retryable_llm_error(self):
        provider = self._provider()
        provider._session = MagicMock()
        provider._session.request.side_effect = RetryExhaustedError(
            "Exceeded 4 retries"
        )
        with pytest.raises(LLMError, match="rate limit not clearing") as info:
            provider.complete("sys", "hello")
        assert not isinstance(info.value, LLMFatalError)

    def test_preflight_skipped_on_mock_base_url(self):
        provider = self._provider()
        provider.preflight()  # no credentials, must not raise

    def test_preflight_refreshes_credentials(self):
        with patch.object(VertexProvider, "_refresh_token") as refresh:
            provider = VertexProvider(
                LLMSettings(
                    provider="vertex",
                    model="m",
                    vertex_project="proj",
                    vertex_region="global",
                )
            )
            provider._credentials = MagicMock()
            provider.preflight()
        assert refresh.call_count == 2  # __init__ + preflight


class TestReasoningParams:
    def test_thinking_family_off_by_default(self):
        assert reasoning_params("claude-sonnet-5", "medium") == {
            "thinking": {"type": "disabled"},
            "output_config": {"effort": "medium"},
        }

    def test_adaptive_when_asked(self):
        assert reasoning_params("claude-sonnet-5", "medium", "adaptive") == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
        }

    @pytest.mark.parametrize(
        "model", ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-8", "m"]
    )
    def test_other_models_untouched(self, model):
        assert reasoning_params(model, "low") == {}


class TestLoadProvider:
    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown llm.provider"):
            load_provider(LLMSettings(provider="nope", model="m"))

    def test_builtin_providers_resolve(self):
        settings = LLMSettings(
            provider="anthropic", model="m", base_url="http://localhost:9999"
        )
        with patch.dict(os.environ, {}, clear=True):
            assert isinstance(load_provider(settings), AnthropicProvider)
        vertex_settings = LLMSettings(
            provider="vertex",
            model="m",
            vertex_project="p",
            base_url="http://localhost:9999",
        )
        assert isinstance(load_provider(vertex_settings), VertexProvider)

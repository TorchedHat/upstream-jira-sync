from __future__ import annotations

from typing import Any

from upstream_jira_sync.models import PullRequest, repo_from_github_url

_ADF_BLOCK_TYPES = {"paragraph", "heading", "listItem", "codeBlock", "blockquote"}


def adf_to_text(adf: Any) -> str:
    """Flatten an ADF tree to plain text, separating blocks with newlines."""
    if not isinstance(adf, dict):
        return ""
    parts: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        text = node.get("text")
        if isinstance(text, str):
            parts.append(text)
        for child in node.get("content") or []:
            walk(child)
        if node.get("type") in _ADF_BLOCK_TYPES:
            parts.append("\n")

    walk(adf)
    return "".join(parts).strip()


def _sentence(text: str) -> str:
    """Trim and terminate free-form model text so it reads as a sentence."""
    text = " ".join(text.split())
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _match_summary(confidence: str, reason: str) -> str:
    """Plain-language explanation of why the PR was linked to this ticket.

    Internal verdict tokens (``high``, ``state``, ``auto``) never reach Jira;
    each maps to a short sentence a reader can act on.
    """
    if not confidence:
        return ""
    if confidence == "auto":
        return (
            "This ticket was created automatically to track the upstream PR, "
            "which had no existing tracking ticket."
        )
    if confidence == "state":
        return (
            "Linked to this ticket automatically. "
            "The PR was already tracked here in an earlier run."
        )
    lead = "Linked to this ticket automatically."
    if reason:
        return f"{lead} {_sentence(reason)}"
    return lead


class AdfBuilder:
    """Constructs Atlassian Document Format (ADF) payloads for Jira API v3."""

    @staticmethod
    def pr_comment(
        pr: PullRequest,
        status_name: str,
        *,
        match_confidence: str = "",
        match_reason: str = "",
        note: str = "",
    ) -> dict:
        """Build the ADF body for a PR link comment."""
        repo = repo_from_github_url(pr.url)
        content: list[dict] = [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{pr.status_icon} Upstream PR: "
                            f"{repo}#{pr.number} — {pr.title}"
                        ),
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {"href": pr.url},
                            }
                        ],
                    }
                ],
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Jira status set to {status_name}. "
                            f"PR last updated {pr.updated_date}."
                        ),
                    }
                ],
            },
        ]
        summary = _match_summary(match_confidence, match_reason)
        if summary:
            content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": summary}],
                }
            )
        if note:
            content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": note}],
                }
            )
        return {"body": {"type": "doc", "version": 1, "content": content}}

    @staticmethod
    def note(text: str) -> dict:
        """Build the ADF body for a plain one-paragraph comment (no PR context)."""
        return {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": text}],
                    }
                ],
            }
        }

    @staticmethod
    def mention_note(
        before: str, account_id: str, after: str, display_name: str = ""
    ) -> dict:
        """One-paragraph comment with a real user mention (Jira renders the display name).

        display_name fills the mention's text attr, matching the node shape the Jira UI
        produces; when empty, Jira hydrates the name from the accountId."""
        attrs: dict = {"id": account_id}
        if display_name:
            attrs["text"] = f"@{display_name}"
        content: list[dict] = []
        if before:
            content.append({"type": "text", "text": before})
        content.append({"type": "mention", "attrs": attrs})
        if after:
            content.append({"type": "text", "text": after})
        return {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": content}],
            }
        }

    @staticmethod
    def issue_description(issue_title: str, issue_url: str, summary: str) -> dict:
        """Build ADF description for an auto-created ticket from a GitHub Issue."""
        return AdfBuilder._linked_description(
            f"GitHub Issue: {issue_title}", issue_url, summary
        )

    @staticmethod
    def pr_description(pr_title: str, pr_url: str, summary: str) -> dict:
        """Build ADF description for an auto-created ticket tracking a GitHub PR."""
        return AdfBuilder._linked_description(
            f"Upstream PR: {pr_title}", pr_url, summary
        )

    @staticmethod
    def _linked_description(heading: str, url: str, summary: str) -> dict:
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": heading,
                            "marks": [
                                {"type": "link", "attrs": {"href": url}},
                            ],
                        },
                    ],
                },
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": summary or "No summary available."},
                    ],
                },
            ],
        }

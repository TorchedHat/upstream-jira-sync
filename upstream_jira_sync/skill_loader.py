from __future__ import annotations

import os
import re
from importlib import resources
from typing import Final


def strip_markdown_fences(text: str) -> str:
    """Remove accidental markdown code fences from model responses."""
    if text.startswith("```"):
        return re.sub(r"^```[a-z]*\n?", "", text).rstrip("`").strip()
    return text


def is_bot_author(email: str | None, bot_email: str) -> bool:
    """True if the given email matches the bot's service account (case-insensitive)."""
    if not email or not bot_email:
        return False
    return email.strip().lower() == bot_email.strip().lower()


def is_bot_actor(
    author: dict | None,
    bot_email: str,
    bot_account_id: str = "",
    aliases: tuple[str, ...] = (),
) -> bool:
    """True if a Jira changelog history.author dict represents the bot service account.

    None/empty author = system event, returns False (caller must NOT treat as human).
    aliases entries may be emails or accountIds; matched case-insensitively for emails.
    """
    if not author:
        return False

    email = (author.get("emailAddress") or "").strip().lower()
    account_id = (author.get("accountId") or "").strip()

    bot_email_norm = bot_email.strip().lower() if bot_email else ""
    bot_account_id_norm = bot_account_id.strip() if bot_account_id else ""

    if email and bot_email_norm and email == bot_email_norm:
        return True
    if account_id and bot_account_id_norm and account_id == bot_account_id_norm:
        return True

    for alias in aliases:
        if not alias:
            continue
        alias_stripped = alias.strip()
        if email and alias_stripped.lower() == email:
            return True
        if account_id and alias_stripped == account_id:
            return True

    return False


SKILL_TEMPLATE_VARS: Final[dict[str, frozenset[str]]] = {
    "ticket_matcher": frozenset(),
    "story_point_estimation": frozenset(),
    "issue_summarizer": frozenset(),
    "issue_claim_classifier": frozenset(),
    "issue_dedup_matcher": frozenset(),
    "rfc_classifier": frozenset(),
    "discussion_summarizer": frozenset(),
    "weekly_digest_summary": frozenset(),
    "review_activity_intro": frozenset(),
    "team_classification": frozenset(
        {"teams_section", "pr_title", "pr_body", "file_paths"}
    ),
    "low_conf_email": frozenset({"github", "pr_number", "pr_title", "pr_url"}),
}


class SkillLoader:
    """Loads prompt content from packaged skill files, with optional per-file
    overrides from settings.skills_dir (R13)."""

    def __init__(self, override_dir: str = "") -> None:
        self._override_dir = override_dir
        self._cache: dict[str, str] = {}

    def load(self, skill_name: str) -> str:
        """Load {skill_name}.md (override dir first, else packaged default),
        stripping YAML frontmatter. Overrides are validated for the template
        variables the framework injects."""
        if skill_name in self._cache:
            return self._cache[skill_name]

        content, is_override = self._read(skill_name)
        content = _strip_frontmatter(content)
        if is_override:
            _validate_override(skill_name, content)
        self._cache[skill_name] = content
        return content

    def _read(self, skill_name: str) -> tuple[str, bool]:
        if self._override_dir:
            path = os.path.join(self._override_dir, f"{skill_name}.md")
            if os.path.isfile(path):
                with open(path) as f:
                    return f.read(), True
        packaged = resources.files("upstream_jira_sync") / "skills" / f"{skill_name}.md"
        if not packaged.is_file():
            raise FileNotFoundError(
                f"Skill '{skill_name}' not found in packaged skills"
                + (f" or {self._override_dir}/" if self._override_dir else "")
            )
        return packaged.read_text(), False


def _strip_frontmatter(content: str) -> str:
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3 :].strip()
    return content


def _validate_override(skill_name: str, content: str) -> None:
    required = SKILL_TEMPLATE_VARS.get(skill_name, frozenset())
    missing = sorted(v for v in required if f"{{{v}}}" not in content)
    if missing:
        raise ValueError(
            f"skill override '{skill_name}' is missing template variables: "
            f"{{{', '.join(missing)}}}"
        )

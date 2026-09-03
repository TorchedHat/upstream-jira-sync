"""
Mock server for integration testing without real GitHub, Jira, or LLM APIs.

Simulates:
  - GitHub GraphQL + REST (PR search)
  - Jira REST API v3 (search, comments, transitions, field updates)
  - LLM providers (Anthropic Messages API and Vertex rawPredict paths)

Usage:
  python3 tests/mock_server.py                # starts on port 9999
  python3 tests/mock_server.py --port 8888    # custom port

Then run the sync pointed at it:
  upstream-jira-sync sync --config tests/fixtures/config.test.yaml \
      --mock-url http://localhost:9999 --dry-run
"""

import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

PROJECT_KEY = "PROJ"

MOCK_TICKETS = [
    {
        "key": f"{PROJECT_KEY}-101",
        "fields": {
            "summary": "Implement retry logic in the transport client",
            "status": {"name": "To Do"},
            "labels": [],
            "issuetype": {"name": "Story"},
            "description": None,
        },
    },
    {
        "key": f"{PROJECT_KEY}-102",
        "fields": {
            "summary": "Fix numerical accuracy in the aggregation pipeline",
            "status": {"name": "In Progress"},
            "labels": [],
            "issuetype": {"name": "Story"},
            "description": None,
        },
    },
    {
        "key": f"{PROJECT_KEY}-103",
        "fields": {
            "summary": "Add memory profiling support to benchmarks",
            "status": {"name": "In Progress"},
            "labels": [],
            "issuetype": {"name": "Story"},
            "description": None,
        },
    },
]

# Transition labels deliberately differ from target status names: the client
# must resolve by to.name / to.id (R4), never by the transition's own name.
MOCK_TRANSITIONS = [
    {"id": "11", "name": "Back to backlog", "to": {"id": "1", "name": "To Do"}},
    {"id": "21", "name": "Start work", "to": {"id": "2", "name": "In Progress"}},
    {"id": "31", "name": "Request review", "to": {"id": "3", "name": "In Review"}},
    {"id": "41", "name": "Finish", "to": {"id": "4", "name": "Done"}},
]

TICKET_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
AUTHOR_RE = re.compile(r"author:(\S+)")

REQUEST_LOG: list[dict] = []


def _mock_pr_node(author: str) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "number": 42,
        "title": "Implement retry logic in the transport client",
        "url": "https://github.com/exampleorg/widgets/pull/42",
        "state": "OPEN",
        "isDraft": False,
        "merged": False,
        "updatedAt": now,
        "body": "Adds exponential backoff to the transport client.",
        "author": {"login": author},
        "labels": {"nodes": []},
        "reviewDecision": None,
        "reviews": {"nodes": []},
        "closingIssuesReferences": {"nodes": []},
    }


class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        REQUEST_LOG.append({"method": "GET", "path": self.path})

        if "/rest/api/3/search" in self.path:
            self._json_response({"issues": MOCK_TICKETS})
            return

        if re.search(r"/rest/api/3/issue/.+/transitions", self.path):
            self._json_response({"transitions": MOCK_TRANSITIONS})
            return

        if re.search(r"/rest/api/3/issue/.+/remotelink", self.path):
            self._json_response([])
            return

        if "/rest/api/3/user/search" in self.path:
            self._json_response([{"accountId": "mock-account-id"}])
            return

        # Anthropic model lookup, used by AnthropicProvider.preflight()
        m = re.search(r"/v1/models/([^/?]+)$", self.path)
        if m:
            self._json_response({"type": "model", "id": m.group(1)})
            return

        self._json_response({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = self._read_body()
        REQUEST_LOG.append({"method": "POST", "path": self.path, "body": body})

        if self.path.rstrip("/").endswith("/graphql"):
            self._handle_github_graphql(body)
            return

        # LLM: Anthropic Messages API or Vertex rawPredict path
        if "/v1/messages" in self.path or "/publishers/anthropic/models/" in self.path:
            self._handle_llm(body)
            return

        if re.search(r"/rest/api/3/issue/.+/comment", self.path):
            self._json_response({"id": "10001"}, 201)
            return

        if re.search(r"/rest/api/3/issue/.+/remotelink", self.path):
            self._json_response({"id": 10002}, 201)
            return

        if re.search(r"/rest/api/3/issue/.+/transitions", self.path):
            self._json_response({}, 204)
            return

        if self.path.split("?")[0].rstrip("/").endswith("/rest/api/3/issue"):
            self._json_response({"key": f"{PROJECT_KEY}-999"}, 201)
            return

        self._json_response({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        body = self._read_body()
        REQUEST_LOG.append({"method": "PUT", "path": self.path, "body": body})

        if re.search(r"/rest/api/3/issue/.+", self.path):
            self._json_response({}, 204)
            return

        self._json_response({"error": "not found"}, 404)

    def _handle_github_graphql(self, body: dict) -> None:
        query = body.get("query", "")
        variables = body.get("variables", {}) or {}
        empty_search = {
            "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            }
        }

        if "closingIssuesReferences" in query:
            author_match = AUTHOR_RE.search(variables.get("query", ""))
            author = author_match.group(1) if author_match else "octocat"
            data = {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [_mock_pr_node(author)],
                }
            }
        elif "pullRequest(number" in query:
            data = {"repository": {"pullRequest": None}}
        elif "issue(number" in query:
            data = {"repository": {"issue": None}}
        else:
            data = empty_search

        self._json_response({"data": data})

    def _handle_llm(self, body: dict) -> None:
        """Route to matching or estimation based on prompt content."""
        system = body.get("system", "")
        user_msg = ""
        for msg in body.get("messages", []):
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
        haystack = f"{system}\n{user_msg}".lower()

        if "story point" in haystack or "scoring checklist" in haystack:
            response_text = json.dumps(
                {
                    "points": 5,
                    "reason": "Cross-module change with moderate integration and test burden",
                }
            )
        else:
            match = TICKET_KEY_RE.search(user_msg)
            key = match.group(1) if match else f"{PROJECT_KEY}-101"
            response_text = json.dumps(
                {
                    "key": key,
                    "confidence": "high",
                    "reason": "PR title closely matches ticket goal",
                }
            )

        self._json_response(
            {
                "content": [{"type": "text", "text": response_text}],
                "model": "mock-model",
                "stop_reason": "end_turn",
            }
        )

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", errors="replace")}

    def _json_response(self, data, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, fmt, *args) -> None:
        method = args[0].split()[0] if args else ""
        colors = {"GET": "\033[32m", "POST": "\033[33m", "PUT": "\033[36m"}
        reset = "\033[0m"
        color = colors.get(method, "")
        print(f"  {color}{fmt % args}{reset}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mock server for upstream-jira-sync testing"
    )
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), MockHandler)
    print(f"Mock server running on http://127.0.0.1:{args.port}")
    print(f"  GitHub API: http://127.0.0.1:{args.port}/graphql")
    print(f"  Jira API:   http://127.0.0.1:{args.port}/rest/api/3/...")
    print(f"  LLM API:    http://127.0.0.1:{args.port}/v1/messages")
    print()
    print("Mock tickets:")
    for t in MOCK_TICKETS:
        print(
            f"  {t['key']}: {t['fields']['summary']} [{t['fields']['status']['name']}]"
        )
    print()
    print("Waiting for requests... (Ctrl+C to stop)")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nRequest log:")
        for r in REQUEST_LOG:
            print(f"  {r['method']} {r['path']}")
        server.server_close()


if __name__ == "__main__":
    main()

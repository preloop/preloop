#!/usr/bin/env python
"""Generate the bundled example-session transcript used by the Optimize tab.

The output is committed as
``backend/preloop/services/data/example_session.json`` and is the *only*
input to :mod:`preloop.services.example_optimization`. Regenerate with::

    PRELOOP_DISABLE_TELEMETRY=true python scripts/generate_example_session.py

Why a generator instead of a hand-written fixture: the transcript has to be
bulky enough to cross the real analyzer thresholds (repeated log lines,
homogeneous JSON arrays, oversized result fields, re-sent tool schemas).
Hand-maintaining ~100KB of realistic JSON is not reviewable; a generator is.

Provenance note (important for honesty): this transcript is a *constructed*
example modelled on the shape of a real CI-triage agent session — an MCP
GitHub server plus filesystem tools. It is not a recording of a real user's
traffic, and nothing here is presented to users as their own data. Every
number the console shows for it is computed from this content by the same
analyzers that run on real sessions; no savings figure is hardcoded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "preloop"
    / "services"
    / "data"
    / "example_session.json"
)

# Claude Sonnet 4 public list pricing, USD per token. Used to price the
# example's measured token counts so the dollar figure is derived, not invented.
INPUT_COST_PER_TOKEN = 3.00 / 1_000_000
OUTPUT_COST_PER_TOKEN = 15.00 / 1_000_000

MODEL_ALIAS = "anthropic/claude-sonnet-4"
PROVIDER_NAME = "anthropic"

SYSTEM_PROMPT = (
    "You are a release engineering agent operating on the acme/checkout "
    "repository. Your job is to triage failing CI pipelines, identify the "
    "root cause, and report a concise summary to the on-call engineer.\n\n"
    "Operating rules:\n"
    "- Always inspect the most recent failing workflow run before forming a "
    "hypothesis.\n"
    "- Prefer reading source files over guessing at behaviour.\n"
    "- Never modify CI configuration without explicit human approval.\n"
    "- When you have a root cause, state it in one sentence, then list the "
    "evidence you used.\n"
)


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Build one OpenAI-style tool definition."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [key for key in properties],
            },
        },
    }


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


# Nine advertised tools. Three are actually invoked; six are dead weight whose
# schemas are re-sent on every request -> drives the "scope down tools"
# suggestion with a genuinely measured token figure.
TOOLS_FULL: list[dict[str, Any]] = [
    _tool(
        "github__list_workflow_runs",
        "List recent GitHub Actions workflow runs for a repository, including "
        "status, conclusion, timing, triggering actor, and the head commit "
        "associated with each run. Supports filtering by branch and event.",
        {
            "repo": _string("Repository in owner/name form."),
            "branch": _string("Branch to filter runs by."),
            "status": _string("Run status filter, e.g. completed or failure."),
        },
    ),
    _tool(
        "github__get_workflow_run_logs",
        "Fetch the raw log output for a single GitHub Actions workflow run. "
        "Returns the concatenated step logs as plain text, which may be large "
        "for long-running or highly parallel pipelines.",
        {
            "repo": _string("Repository in owner/name form."),
            "run_id": _string("Numeric workflow run identifier."),
        },
    ),
    _tool(
        "fs__read_file",
        "Read a UTF-8 text file from the local checkout and return its full "
        "contents. Use this to inspect source, configuration, and lockfiles "
        "before forming a hypothesis about a failure.",
        {"path": _string("Repository-relative path to the file to read.")},
    ),
    _tool(
        "github__create_issue",
        "Open a new GitHub issue in the target repository with a title, "
        "markdown body, assignees, labels, and an optional milestone. Use "
        "this to file a defect once a root cause has been confirmed by a "
        "human reviewer.",
        {
            "repo": _string("Repository in owner/name form."),
            "title": _string("Issue title."),
            "body": _string("Markdown issue body."),
            "labels": _string("Comma-separated label names."),
            "assignees": _string("Comma-separated GitHub usernames."),
        },
    ),
    _tool(
        "github__update_issue",
        "Update an existing GitHub issue: change its title, body, state, "
        "labels, assignees, or milestone. Commonly used to close a triage "
        "issue once the underlying pipeline failure has been resolved.",
        {
            "repo": _string("Repository in owner/name form."),
            "issue_number": _string("Issue number to update."),
            "state": _string("New issue state, open or closed."),
            "body": _string("Replacement markdown issue body."),
        },
    ),
    _tool(
        "github__list_pull_requests",
        "List open or closed pull requests for a repository including title, "
        "author, target branch, mergeability, review state, and the set of "
        "files each pull request touches.",
        {
            "repo": _string("Repository in owner/name form."),
            "state": _string("Pull request state filter."),
            "base": _string("Base branch to filter by."),
        },
    ),
    _tool(
        "github__merge_pull_request",
        "Merge an approved pull request using the repository's configured "
        "merge strategy. Fails if required status checks have not passed or "
        "if the branch is behind its base.",
        {
            "repo": _string("Repository in owner/name form."),
            "pull_number": _string("Pull request number to merge."),
            "merge_method": _string("One of merge, squash, or rebase."),
        },
    ),
    _tool(
        "slack__post_message",
        "Post a message to a Slack channel or thread on behalf of the agent, "
        "with optional blocks, attachments, and thread targeting. Used to "
        "notify the on-call engineer once triage has completed.",
        {
            "channel": _string("Slack channel id or name."),
            "text": _string("Message text in Slack markdown."),
            "thread_ts": _string("Optional parent thread timestamp."),
        },
    ),
    _tool(
        "jira__create_ticket",
        "Create a Jira ticket in the configured project with a summary, "
        "description, issue type, priority, and component assignment. Used "
        "when the team tracks defects in Jira rather than GitHub issues.",
        {
            "project": _string("Jira project key."),
            "summary": _string("Ticket summary line."),
            "description": _string("Ticket description body."),
            "issue_type": _string("Issue type, e.g. Bug or Task."),
        },
    ),
]

# On the third request the agent narrows its tool set. Changing the advertised
# tools mid-session invalidates the provider's cached prefix -> drives the
# "stabilize the prefix" suggestion from a real, detected divergence.
TOOLS_NARROWED = TOOLS_FULL[:3]


def _workflow_runs_result() -> str:
    """A homogeneous array of workflow runs with bulky per-item fields.

    Twelve near-identical objects trip the homogeneous-array compressibility
    analyzer, and the repeated ``logs_url`` / ``head_commit`` / ``actor``
    fields trip the oversized-output-field analyzer.
    """
    runs: list[dict[str, Any]] = []
    for index in range(12):
        run_id = 5821000 + index
        failed = index in (0, 3, 7)
        runs.append(
            {
                "id": run_id,
                "name": "CI",
                "node_id": f"WFR_kwLOA{run_id}zQ",
                "head_branch": "main",
                "head_sha": f"{index:02x}" + "9f2c41b7de3a5480cc17e2b6a8f3d10945e7b2c",
                "run_number": 4120 + index,
                "event": "push",
                "status": "completed",
                "conclusion": "failure" if failed else "success",
                "workflow_id": 71223344,
                "created_at": f"2026-07-1{index % 9}T09:1{index % 6}:22Z",
                "updated_at": f"2026-07-1{index % 9}T09:2{index % 6}:07Z",
                "html_url": f"https://github.com/acme/checkout/actions/runs/{run_id}",
                "logs_url": (
                    f"https://api.github.com/repos/acme/checkout/actions/runs/"
                    f"{run_id}/logs?token=AABBCCDDEEFF00112233445566778899"
                    "&expires=1784500000&signature=b7c1e94a2f83d05e6a1c74bf93028dd5"
                ),
                "jobs_url": (
                    f"https://api.github.com/repos/acme/checkout/actions/runs/"
                    f"{run_id}/jobs"
                ),
                "artifacts_url": (
                    f"https://api.github.com/repos/acme/checkout/actions/runs/"
                    f"{run_id}/artifacts"
                ),
                "head_commit": {
                    "id": f"{index:02x}9f2c41b7de3a5480cc17e2b6a8f3d10945e7b2c",
                    "tree_id": f"{index:02x}1a7b93ce02d4f8615a09c3be27d418f0a6c95d3",
                    "message": (
                        "refactor(checkout): extract price rounding into a shared "
                        "helper so the web and mobile carts agree on cents\n\n"
                        "Co-authored-by: Priya Raman <priya@acme.example>"
                    ),
                    "timestamp": f"2026-07-1{index % 9}T09:0{index % 6}:11Z",
                    "author": {
                        "name": "Priya Raman",
                        "email": "priya@acme.example",
                    },
                    "committer": {
                        "name": "GitHub",
                        "email": "noreply@github.com",
                    },
                },
                "actor": {
                    "login": "priya-raman",
                    "id": 4471290 + index,
                    "node_id": "MDQ6VXNlcjQ0NzEyOTA=",
                    "avatar_url": (
                        "https://avatars.githubusercontent.com/u/4471290?v=4"
                    ),
                    "gravatar_id": "",
                    "url": "https://api.github.com/users/priya-raman",
                    "html_url": "https://github.com/priya-raman",
                    "followers_url": (
                        "https://api.github.com/users/priya-raman/followers"
                    ),
                    "organizations_url": (
                        "https://api.github.com/users/priya-raman/orgs"
                    ),
                    "repos_url": "https://api.github.com/users/priya-raman/repos",
                    "type": "User",
                    "site_admin": False,
                },
            }
        )
    return json.dumps(runs, ensure_ascii=False)


def _run_logs_result() -> str:
    """Raw CI logs with heavily repeated lines (retry storm + install noise)."""
    lines: list[str] = [
        "2026-07-18T09:14:22.101Z ##[group]Run actions/checkout@v4",
        "2026-07-18T09:14:22.884Z Syncing repository: acme/checkout",
        "2026-07-18T09:14:23.402Z ##[endgroup]",
        "2026-07-18T09:14:24.019Z ##[group]Run npm ci",
    ]
    # An npm install that resolves the same transitive dep over and over.
    for _ in range(18):
        lines.append(
            "2026-07-18T09:14:31.226Z npm warn deprecated "
            "inflight@1.0.6: This module is not supported, and leaks memory."
        )
    for _ in range(14):
        lines.append(
            "2026-07-18T09:14:39.881Z npm warn deprecated "
            "glob@7.2.3: Glob versions prior to v9 are no longer supported."
        )
    lines.append("2026-07-18T09:15:02.550Z added 1284 packages in 38s")
    lines.append("2026-07-18T09:15:03.001Z ##[endgroup]")
    lines.append("2026-07-18T09:15:03.400Z ##[group]Run npm test -- --runInBand")
    # A flaky suite retried repeatedly by the test runner.
    for _ in range(11):
        lines.append(
            "2026-07-18T09:15:44.318Z   RETRY 1 checkout/cart total rounds "
            "line items to the nearest cent"
        )
    lines.extend(
        [
            "2026-07-18T09:16:12.771Z   FAIL  src/cart/total.test.ts",
            "2026-07-18T09:16:12.772Z     ● cart total rounds line items to "
            "the nearest cent",
            "2026-07-18T09:16:12.774Z       expect(received).toBe(expected)",
            "2026-07-18T09:16:12.775Z       Expected: 1999",
            "2026-07-18T09:16:12.776Z       Received: 1998",
            "2026-07-18T09:16:12.780Z       at Object.<anonymous> "
            "(src/cart/total.test.ts:42:26)",
            "2026-07-18T09:16:13.020Z Tests: 1 failed, 316 passed, 317 total",
            "2026-07-18T09:16:13.402Z ##[error]Process completed with exit code 1.",
        ]
    )
    return "\n".join(lines)


# Read twice by the agent (once to inspect rounding, once to re-check after
# reading the logs) -> identical content drives the deduplication suggestion.
TOTAL_TS_SOURCE = (
    "import { Money } from '../money';\n"
    "import type { LineItem } from './types';\n"
    "\n"
    "/**\n"
    " * Sum line items into an order total.\n"
    " *\n"
    " * Historically this rounded each line item independently, which drifts\n"
    " * from the mobile client's behaviour of rounding once at the end.\n"
    " */\n"
    "export function cartTotal(items: LineItem[]): Money {\n"
    "  let subtotalCents = 0;\n"
    "  for (const item of items) {\n"
    "    // NOTE: per-line rounding is the suspected source of the off-by-one.\n"
    "    const lineCents = Math.round(item.unitPrice * item.quantity * 100);\n"
    "    subtotalCents += lineCents;\n"
    "  }\n"
    "  const taxCents = Math.round(subtotalCents * TAX_RATE);\n"
    "  const shippingCents = shippingFor(subtotalCents);\n"
    "  return Money.fromCents(subtotalCents + taxCents + shippingCents);\n"
    "}\n"
    "\n"
    "const TAX_RATE = 0.0825;\n"
    "\n"
    "function shippingFor(subtotalCents: number): number {\n"
    "  if (subtotalCents >= 5000) return 0;\n"
    "  return 599;\n"
    "}\n"
)


def _build_messages() -> list[list[dict[str, Any]]]:
    """Return the accumulated message list for each of the three requests."""
    user_task = {
        "role": "user",
        "content": (
            "The main branch CI has been red since this morning. Figure out "
            "what is failing and why, then summarise the root cause for the "
            "on-call engineer."
        ),
    }
    system = {"role": "system", "content": SYSTEM_PROMPT}

    # Turn 1: agent asks for recent runs.
    turn1 = [system, user_task]

    # Turn 2: it has the runs + the logs for the failing one, and has read the
    # source file once.
    turn2 = turn1 + [
        {
            "role": "assistant",
            "content": "Let me look at the recent workflow runs on main.",
            "tool_calls": [
                {
                    "id": "call_runs_1",
                    "type": "function",
                    "function": {
                        "name": "github__list_workflow_runs",
                        "arguments": json.dumps(
                            {
                                "repo": "acme/checkout",
                                "branch": "main",
                                "status": "completed",
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_runs_1",
            "name": "github__list_workflow_runs",
            "content": _workflow_runs_result(),
        },
        {
            "role": "assistant",
            "content": "Run 5821000 failed. Pulling its logs.",
            "tool_calls": [
                {
                    "id": "call_logs_1",
                    "type": "function",
                    "function": {
                        "name": "github__get_workflow_run_logs",
                        "arguments": json.dumps(
                            {"repo": "acme/checkout", "run_id": "5821000"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_logs_1",
            "name": "github__get_workflow_run_logs",
            "content": _run_logs_result(),
        },
    ]

    # Turn 3: it reads the same source file twice across the conversation.
    turn3 = turn2 + [
        {
            "role": "assistant",
            "content": "The cart total test fails by one cent. Reading the source.",
            "tool_calls": [
                {
                    "id": "call_read_1",
                    "type": "function",
                    "function": {
                        "name": "fs__read_file",
                        "arguments": json.dumps({"path": "src/cart/total.ts"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read_1",
            "name": "fs__read_file",
            "content": TOTAL_TS_SOURCE,
        },
        {
            "role": "assistant",
            "content": "Re-reading the rounding helper to confirm the drift.",
            "tool_calls": [
                {
                    "id": "call_read_2",
                    "type": "function",
                    "function": {
                        "name": "fs__read_file",
                        "arguments": json.dumps({"path": "src/cart/total.ts"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_read_2",
            "name": "fs__read_file",
            "content": TOTAL_TS_SOURCE,
        },
        {
            "role": "user",
            "content": "Good — what is the root cause?",
        },
    ]

    return [turn1, turn2, turn3]


def build_transcript() -> dict[str, Any]:
    """Assemble the committed example-session document."""
    turns = _build_messages()
    tool_sets = [TOOLS_FULL, TOOLS_FULL, TOOLS_NARROWED]
    completions = [140, 210, 320]

    events: list[dict[str, Any]] = []
    for index, (messages, tools, completion_tokens) in enumerate(
        zip(turns, tool_sets, completions, strict=True)
    ):
        events.append(
            {
                "event_id": f"example-event-{index + 1}",
                "request": {"messages": messages, "tools": tools},
                # prompt_tokens is filled in by the loader from the measured
                # content so the fixture can never drift from its own numbers.
                "completion_tokens": completion_tokens,
                "outcome": "success",
                "status_code": 200,
                "model_alias": MODEL_ALIAS,
                "provider_name": PROVIDER_NAME,
            }
        )

    return {
        "schema_version": 1,
        "session_reference": "example-ci-triage",
        "title": "Example: CI failure triage (bundled sample)",
        "provenance": (
            "Constructed example modelled on a CI-triage agent session using an "
            "MCP GitHub server plus filesystem tools. Not a recording of real "
            "user traffic. All reported savings are computed from this content "
            "by the same analyzers Preloop runs on real sessions."
        ),
        "model_alias": MODEL_ALIAS,
        "provider_name": PROVIDER_NAME,
        "input_cost_per_token": INPUT_COST_PER_TOKEN,
        "output_cost_per_token": OUTPUT_COST_PER_TOKEN,
        "pricing_note": "Priced at Claude Sonnet 4 public list rates.",
        "events": events,
    }


def main() -> None:
    """Write the transcript to its committed location."""
    document = build_transcript()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()

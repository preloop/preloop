#!/usr/bin/env python
"""Seed a synthetic software dev+ops team for demos and cost profiling.

Usage::

    DATABASE_URL=postgresql://... DEMO_TEAM_PASSWORD=... \\
        python scripts/seed_demo_team.py [--days 28] [--scale 1] [--seed 1701]
    python scripts/seed_demo_team.py --database-url postgresql://... --reset
    # keep runners "Online" and agents/devices live while recording:
    python scripts/seed_demo_team.py --database-url postgresql://... --no-seed --keepalive

What it creates, all inside ONE account ("Enterprise Labs (demo)"):

* Members Nyota Uhura (the owner login, ``uhura@example.com``) and
  Montgomery Scott (``scotty@example.com``, no password).
* Devices ``uhura-mbp`` and ``scotty-xps`` (expected-device roster entries
  whose fingerprints match the runners), a shared hosted runner and each
  member's personal runner, with owner and host name set, each reporting a
  harness inventory and a fresh heartbeat. Presence lapses 45 s after the
  last heartbeat; ``--keepalive`` refreshes runner heartbeats and agent
  last-seen times every 15 s until Ctrl-C.
* Managed agents Claude Code, Codex and Copilot owned by the two members.
* Flows for issue triage, implement, review, repair and deploy.
* A GitHub tracker for ``enterprise-labs/warp-core`` with 25 issues and 20
  pull requests, most of which close an issue (some issues get an implement
  and a repair pull request).
* ``--days`` of flow executions and agent runtime sessions with tool call,
  model and approval activity, priced gateway usage rows across several
  models, issue cost rollups/facts/pull requests, and ``work_ref`` tool calls
  shaped by ``preloop.services.fleet_work_refs``. Triage and implement runs
  carry a dozen execution log steps each, and agent sessions extend their
  agent's source id so the Users tab attributes them to the owner.
* Budget policies with spend history: one healthy, one near its limit and
  one over it.

``--scale`` multiplies the activity volume: 1 gives roughly 5-10k usage
rows, 10 gives roughly 50-100k for performance profiling.

Idempotency: every row id is a uuid5 under a fixed namespace and the demo
account is removed (with everything it owns) before it is seeded again, so a
rerun replaces the data instead of duplicating it. ``--reset`` only removes
the demo account. Randomness comes from ``random.Random(--seed)``.

Every person, email, host, repository and issue in this dataset is synthetic.
The password is read from ``--password`` or ``DEMO_TEAM_PASSWORD`` and is
never printed. All database access goes through ``preloop.models.crud``.
"""

from __future__ import annotations

import hashlib
import os
import random
import secrets
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Sequence

import click
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_password_hash
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_usage,
    crud_budget_policy,
    crud_budget_spend,
    crud_expected_device,
    crud_flow,
    crud_flow_execution,
    crud_flow_execution_log,
    crud_flow_runner,
    crud_issue,
    crud_issue_cost,
    crud_managed_agent,
    crud_organization,
    crud_person,
    crud_project,
    crud_role,
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_tracker,
    crud_user,
    crud_user_role,
)
from preloop.models.crud.base import CRUDBase
from preloop.models.crud.budget import get_period_start
from preloop.models.db.session import get_db_session
from preloop.models.db.setup import setup_database
from preloop.models.models.base import Base
from preloop.models.models.budget import BudgetPeriod
from preloop.services.fleet_work_refs import classify_tool_call

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://demo.preloop.invalid/team")
ACCOUNT_NAME = "Enterprise Labs (demo)"
OWNER = "enterprise-labs"
REPO = "warp-core"
REPO_PATH = f"{OWNER}/{REPO}"
GITHUB = "https://github.com"
ISSUE_COUNT = 25
PR_COUNT = 20
FIRST_ISSUE = 101
FIRST_PR = FIRST_ISSUE + ISSUE_COUNT


def sid(*parts: object) -> uuid.UUID:
    """Return the stable demo id for ``parts``."""
    return uuid.uuid5(NAMESPACE, "/".join(str(p) for p in parts))


ACCOUNT_ID = sid("account")


@dataclass(frozen=True)
class Member:
    """One synthetic team member."""

    key: str
    full_name: str
    email: str
    host: str
    platform: str
    os: str
    arch: str


@dataclass(frozen=True)
class Price:
    """Synthetic list price per million tokens."""

    alias: str
    provider: str
    identifier: str
    input_usd: float
    output_usd: float
    cache_read_usd: float


@dataclass(frozen=True)
class AgentSpec:
    """One managed agent and the models it calls."""

    key: str
    display_name: str
    kind: str
    owner: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class FlowSpec:
    """One flow and the model it runs on."""

    key: str
    name: str
    agent_type: str
    model: str
    link: Optional[str]


MEMBERS = (
    Member(
        "uhura",
        "Nyota Uhura",
        "uhura@example.com",
        "uhura-mbp",
        "macos",
        "darwin",
        "arm64",
    ),
    Member(
        "scotty",
        "Montgomery Scott",
        "scotty@example.com",
        "scotty-xps",
        "linux",
        "linux",
        "x86_64",
    ),
)
PRICES = {
    p.alias: p
    for p in (
        Price(
            "anthropic/claude-sonnet-4-5",
            "anthropic",
            "claude-sonnet-4-5",
            3.0,
            15.0,
            0.3,
        ),
        Price("openai/gpt-5-codex", "openai", "gpt-5-codex", 1.25, 10.0, 0.125),
        Price("openai/gpt-4.1", "openai", "gpt-4.1", 2.0, 8.0, 0.5),
        Price("google/gemini-2.5-pro", "google", "gemini-2.5-pro", 1.25, 10.0, 0.31),
    )
}
AGENTS = (
    AgentSpec(
        "claude_code",
        "Claude Code",
        "claude_code",
        "uhura",
        ("anthropic/claude-sonnet-4-5",),
    ),
    AgentSpec("codex", "Codex", "codex", "scotty", ("openai/gpt-5-codex",)),
    AgentSpec(
        "copilot",
        "Copilot",
        "copilot",
        "uhura",
        ("openai/gpt-4.1", "google/gemini-2.5-pro"),
    ),
)
FLOWS = (
    FlowSpec("triage", "Issue triage", "codex", "openai/gpt-4.1", "trigger_issue"),
    FlowSpec(
        "implement",
        "Implement issue",
        "claude_code",
        "anthropic/claude-sonnet-4-5",
        "lifecycle",
    ),
    FlowSpec(
        "review", "Review pull request", "codex", "openai/gpt-5-codex", "pull_request"
    ),
    FlowSpec(
        "repair",
        "Repair failing pull request",
        "claude_code",
        "anthropic/claude-sonnet-4-5",
        "pull_request",
    ),
    FlowSpec("deploy", "Deploy to staging", "codex", "google/gemini-2.5-pro", None),
)
ISSUE_TITLES = (
    "Warp field telemetry drops samples under load",
    "Dilithium cache eviction ignores TTL",
    "Subspace relay retries forever on 503",
    "Plasma conduit metrics missing units",
    "Transporter buffer leaks on cancel",
    "Deflector config reload is not atomic",
    "Sensor sweep job runs twice after deploy",
    "Shuttle bay API returns 500 on empty manifest",
    "Holodeck session tokens never expire",
    "Replicator queue starves low-priority orders",
    "Impulse engine health check flaps",
    "Navigation charts fail to load offline",
    "Comms log export truncates UTF-8",
    "Tractor beam client lacks timeouts",
    "Engineering dashboard shows stale ETA",
    "Bridge alerts duplicate on reconnect",
    "Cargo manifest import rejects CSV BOM",
    "Shield harmonics test is flaky",
    "Turbolift scheduler deadlocks on retry",
    "Stellar cartography search is slow",
    "Crew roster sync drops middle names",
    "Matter stream parser panics on empty frame",
    "Long-range scanner rate limit too strict",
    "Docking clamp audit trail missing actor",
    "Medical bay inventory paging off by one",
)
NATIVE_TOOLS = ("Bash", "Read", "Edit", "Grep", "Write", "WebFetch")


@dataclass
class WorkItem:
    """One synthetic issue or pull request."""

    number: int
    title: str
    created: datetime
    closes: Optional[int] = None
    kind: str = "issue"
    merged: Optional[datetime] = None
    approved: Optional[datetime] = None
    state: str = "open"
    repair: bool = False


@dataclass
class Dataset:
    """Rows to insert, grouped by CRUD object."""

    rows: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    spend: dict[tuple[str, Optional[uuid.UUID], BudgetPeriod, datetime], float] = field(
        default_factory=lambda: defaultdict(float)
    )


def _fit(model: type[Base], row: dict[str, Any]) -> dict[str, Any]:
    """Match datetimes to each column's timezone flag (naive columns are UTC)."""
    mapper_columns = model.__mapper__.columns
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            column = mapper_columns.get(key)
            if column is not None and not getattr(column.type, "timezone", True):
                value = value.astimezone(UTC).replace(tzinfo=None)
        out[key] = value
    return out


def _pr_url(number: int) -> str:
    return f"{GITHUB}/{REPO_PATH}/pull/{number}"


def _issue_url(number: int) -> str:
    return f"{GITHUB}/{REPO_PATH}/issues/{number}"


def _issue_key(number: int) -> str:
    return f"{REPO_PATH}#{number}"


def _fingerprint(host: str) -> str:
    return hashlib.sha256(f"demo-device:{host}".encode()).hexdigest()


def _cost(price: Price, prompt: int, completion: int, cached: int) -> float:
    fresh = max(prompt - cached, 0)
    return round(
        (
            fresh * price.input_usd
            + cached * price.cache_read_usd
            + completion * price.output_usd
        )
        / 1_000_000,
        6,
    )


class Seeder:
    """Builds the demo dataset deterministically and writes it through CRUD."""

    def __init__(
        self,
        db: Session,
        *,
        days: int,
        scale: int,
        seed: int,
        now: Optional[datetime] = None,
    ) -> None:
        self.db = db
        self.days = days
        self.scale = scale
        self.rng = random.Random(seed)
        self.now = (now or datetime.now(UTC)).replace(second=0, microsecond=0)
        self.start = self.now - timedelta(days=days)
        self.data = Dataset()
        self.users: dict[str, uuid.UUID] = {}
        self.agent_ids = {a.key: sid("agent", a.key) for a in AGENTS}
        self.flow_ids = {f.key: sid("flow", f.key) for f in FLOWS}
        self.model_ids = {alias: sid("model", alias) for alias in PRICES}
        self.runner_ids = {
            "hosted": sid("runner", "hosted"),
            **{m.key: sid("runner", m.key) for m in MEMBERS},
        }
        self.tracker_id = sid("tracker")
        self.project_id = sid("project")
        self.issues: list[WorkItem] = []
        self.prs: list[WorkItem] = []
        self.issue_ids = {n: sid("issue", n) for n in range(FIRST_ISSUE, FIRST_PR)}

    # --- helpers ---------------------------------------------------------

    def _when(self, low: datetime, high: Optional[datetime] = None) -> datetime:
        """A random instant in ``[low, high]`` clamped to the window."""
        top = min(high or self.now, self.now)
        low = max(low, self.start)
        if top <= low:
            return low
        return low + timedelta(seconds=self.rng.uniform(0, (top - low).total_seconds()))

    def _usage(
        self,
        *,
        at: datetime,
        alias: str,
        user: str,
        principal: tuple[str, str, str],
        session_id: uuid.UUID,
        auth_subject: str,
        budget_subjects: Sequence[tuple[str, Optional[uuid.UUID]]],
        flow_id: Optional[uuid.UUID] = None,
        execution_id: Optional[uuid.UUID] = None,
        big: bool = False,
    ) -> tuple[int, float]:
        """Append one priced gateway usage row; return its tokens and cost."""
        price = PRICES[alias]
        prompt = int(self.rng.lognormvariate(9.6 if big else 9.0, 0.6))
        completion = int(prompt * self.rng.uniform(0.04, 0.2))
        cached = (
            int(prompt * self.rng.uniform(0.3, 0.8)) if self.rng.random() < 0.7 else 0
        )
        failed = self.rng.random() < 0.02
        cost = 0.0 if failed else _cost(price, prompt, completion, cached)
        total = 0 if failed else prompt + completion
        anthropic = price.provider == "anthropic"
        n = len(self.data.rows["api_usage"])
        self.data.rows["api_usage"].append(
            {
                "id": sid("usage", n),
                "account_id": ACCOUNT_ID,
                "user_id": self.users[user],
                "endpoint": "/v1/messages" if anthropic else "/v1/chat/completions",
                "method": "POST",
                "status_code": 529 if failed else 200,
                "duration": round(self.rng.uniform(0.6, 28.0), 3),
                "action_type": "model_gateway",
                "auth_subject_type": auth_subject,
                "ai_model_id": self.model_ids[alias],
                "flow_id": flow_id,
                "flow_execution_id": execution_id,
                "runtime_session_id": session_id,
                "model_alias": alias,
                "provider_name": price.provider,
                "upstream_request_id": f"req_demo_{n:07d}",
                "prompt_tokens": 0 if failed else prompt,
                "completion_tokens": 0 if failed else completion,
                "total_tokens": total,
                "cache_read_tokens": None if failed else cached,
                "cache_creation_tokens": None if failed else 0,
                "reasoning_tokens": None,
                "estimated_cost": None if failed else cost,
                "currency": None if failed else "USD",
                "cost_source": None if failed else "catalog",
                "usage_source": None,
                "is_retry": False,
                "error_class": "upstream_overloaded" if failed else None,
                "runtime_principal_type": principal[0],
                "runtime_principal_id": principal[1],
                "runtime_principal_name": principal[2],
                "message_count": self.rng.randint(2, 60),
                "tool_call_count": self.rng.randint(0, 6),
                "meta_data": {"demo_seed": True},
                "timestamp": at,
            }
        )
        if cost:
            for subject_type, subject_id in (("account", None), *budget_subjects):
                for period in (
                    BudgetPeriod.daily,
                    BudgetPeriod.weekly,
                    BudgetPeriod.monthly,
                ):
                    start = get_period_start(at, period)
                    assert start is not None
                    self.data.spend[(subject_type, subject_id, period, start)] += cost
        return total, cost

    def _activity(
        self,
        *,
        session_id: uuid.UUID,
        at: datetime,
        activity_type: str,
        status: str,
        tool: Optional[str] = None,
        server: Optional[str] = None,
        summary: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        execution_id: Optional[uuid.UUID] = None,
    ) -> None:
        n = len(self.data.rows["activity"])
        self.data.rows["activity"].append(
            {
                "id": sid("activity", n),
                "account_id": ACCOUNT_ID,
                "runtime_session_id": session_id,
                "flow_execution_id": execution_id,
                "activity_type": activity_type,
                "server_name": server,
                "tool_name": tool,
                "status": status,
                "summary": summary,
                "metadata_": metadata or {},
                "timestamp": at,
            }
        )

    def _work_call(
        self,
        *,
        session_id: uuid.UUID,
        at: datetime,
        tool: str,
        args: dict[str, Any],
        status: str = "succeeded",
        execution_id: Optional[uuid.UUID] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """A GitHub MCP tool call whose ``work_ref`` the classifier produced."""
        work_ref = classify_tool_call("github", tool, args)
        metadata: dict[str, Any] = dict(extra or {})
        if work_ref is not None:
            metadata["work_ref"] = work_ref
        self._activity(
            session_id=session_id,
            at=at,
            activity_type="tool_call",
            status=status,
            tool=tool,
            server="github",
            summary=f"{tool} on {REPO_PATH}",
            metadata=metadata,
            execution_id=execution_id,
        )

    # --- static entities -------------------------------------------------

    def plan_work_items(self) -> None:
        """Lay out 25 issues and 20 pull requests over the window."""
        for i, title in enumerate(ISSUE_TITLES):
            created = self.start + timedelta(
                days=(self.days - 3) * i / ISSUE_COUNT, hours=self.rng.uniform(0, 8)
            )
            self.issues.append(WorkItem(FIRST_ISSUE + i, title, created))
        # PRs 0-14 implement issues 0-14, PRs 15-17 repair issues 0, 3, 6,
        # PRs 18-19 are chores that close nothing.
        closes = list(range(15)) + [0, 3, 6] + [None, None]
        for j, issue_index in enumerate(closes):
            if issue_index is None:
                opened = self._when(self.start + timedelta(days=self.days * j / 25))
                title = ("Bump warp-core dependencies", "Tidy CI cache keys")[j - 18]
                pr = WorkItem(FIRST_PR + j, title, opened, kind="pull_request")
            else:
                issue = self.issues[issue_index]
                lag = timedelta(hours=self.rng.uniform(4, 30))
                if j >= 15:
                    first = self.prs[issue_index]
                    opened = (first.merged or first.created) + timedelta(
                        hours=self.rng.uniform(6, 48)
                    )
                    title = (
                        f"Fix regression from #{first.number}: {issue.title.lower()}"
                    )
                else:
                    opened = issue.created + lag
                    title = f"Fix: {issue.title}"
                pr = WorkItem(
                    FIRST_PR + j,
                    title,
                    min(opened, self.now - timedelta(hours=2)),
                    closes=issue.number,
                    kind="pull_request",
                    repair=j >= 15,
                )
            roll = self.rng.random()
            if roll < 0.7 and pr.created < self.now - timedelta(hours=12):
                pr.approved = pr.created + timedelta(hours=self.rng.uniform(1, 8))
                pr.merged = pr.approved + timedelta(hours=self.rng.uniform(0.2, 4))
                pr.state = "merged"
            elif roll < 0.8:
                pr.state = "closed"
            self.prs.append(pr)
        for pr in self.prs:
            if pr.closes and pr.state == "merged":
                self.issues[pr.closes - FIRST_ISSUE].state = "closed"

    def static_rows(self) -> None:
        """Models, runners, agents, flows, tracker, issues and pull requests."""
        rows = self.data.rows
        for alias, price in PRICES.items():
            rows["ai_model"].append(
                {
                    "id": self.model_ids[alias],
                    "account_id": ACCOUNT_ID,
                    "name": price.identifier,
                    "provider_name": price.provider,
                    "model_identifier": price.identifier,
                    "is_default": alias == "anthropic/claude-sonnet-4-5",
                    "model_parameters": {},
                    "meta_data": {
                        "gateway": {"enabled": True, "model_alias": alias},
                        "pricing": {
                            "input_cost_per_million": price.input_usd,
                            "output_cost_per_million": price.output_usd,
                        },
                    },
                }
            )
        hosted = {
            "id": self.runner_ids["hosted"],
            "account_id": ACCOUNT_ID,
            "registered_by_user_id": self.users["uhura"],
            "name": "warp-ci-hosted",
            "hostname": "warp-ci-01",
            "os": "linux",
            "arch": "x86_64",
            "labels": ["hosted", "linux", "docker"],
            "status": "online",
            "concurrency": 4,
            "reported_concurrency": 4,
            "flow_execution_consent": "always",
            "accept_untagged": True,
        }
        rows["runner"].append(hosted)
        for m in MEMBERS:
            rows["runner"].append(
                {
                    "id": self.runner_ids[m.key],
                    "account_id": ACCOUNT_ID,
                    "registered_by_user_id": self.users[m.key],
                    "name": f"{m.key}-personal",
                    "hostname": m.host,
                    "workstation_fingerprint": _fingerprint(m.host),
                    "os": m.os,
                    "arch": m.arch,
                    "labels": [m.os, "personal"],
                    "status": "online",
                    "concurrency": 2,
                    "reported_concurrency": 2,
                    "flow_execution_consent": "ask_me",
                    "accept_untagged": False,
                }
            )
        for row in rows["runner"]:
            row.update(
                token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                capabilities={"harnesses": ["claude_code", "codex"]},
                publication_capabilities={},
                # Within the 45 s presence window at seed time; keep it there
                # while recording with --keepalive.
                last_heartbeat=self.now,
                harness_inventory=harness_inventory(row["name"], self.now),
                harness_inventory_updated_at=self.now,
            )
        members = {m.key: m for m in MEMBERS}
        for a in AGENTS:
            owner = members[a.owner]
            rows["agent"].append(
                {
                    "id": self.agent_ids[a.key],
                    "account_id": ACCOUNT_ID,
                    "owner_user_id": self.users[a.owner],
                    "agent_kind": a.kind,
                    "session_source_type": a.kind,
                    "session_source_id": f"demo-{a.key}-{owner.host}",
                    "display_name": a.display_name,
                    "enrollment_hostname": owner.host,
                    "enrolled_via": "runtime_session_token",
                    "managed_mcp_servers": ["github", "preloop-mcp"],
                    "tags": {"team": "warp-core"},
                    "lifecycle_state": "active",
                    "lifecycle_updated_at": self.start,
                    "last_seen_at": self.now,
                }
            )
        for f in FLOWS:
            rows["flow"].append(
                {
                    "id": self.flow_ids[f.key],
                    "account_id": ACCOUNT_ID,
                    "name": f.name,
                    "description": f"Synthetic demo flow: {f.name.lower()}.",
                    "prompt_template": f"{f.name} for {{{{ issue.key }}}} in {REPO_PATH}.",
                    "agent_type": f.agent_type,
                    "agent_config": {},
                    "ai_model_id": self.model_ids[f.model],
                    "trigger_event_source": "github",
                    "trigger_event_types": ["issues.opened"]
                    if f.key == "triage"
                    else ["pull_request.opened"],
                    "is_enabled": True,
                    "created_by_user_id": self.users[
                        "uhura" if f.key != "deploy" else "scotty"
                    ],
                }
            )
        for item in self.issues:
            rows["issue"].append(
                {
                    "id": self.issue_ids[item.number],
                    "project_id": self.project_id,
                    "tracker_id": self.tracker_id,
                    "title": item.title,
                    "description": "Synthetic demo issue.",
                    "status": item.state,
                    "priority": self.rng.choice(("low", "medium", "high")),
                    "issue_type": self.rng.choice(("bug", "bug", "feature")),
                    "external_id": str(item.number),
                    "external_url": _issue_url(item.number),
                    "key": _issue_key(item.number),
                    "meta_data": {"demo_seed": True},
                    "last_updated_external": item.created,
                    "created_at": item.created,
                }
            )
        for pr in self.prs:
            rows["pull_request"].append(
                {
                    "id": sid("pr", pr.number),
                    "account_id": ACCOUNT_ID,
                    "pr_key": _pr_url(pr.number),
                    "rollup_id": sid("rollup", pr.closes) if pr.closes else None,
                    "opened_at": pr.created,
                    "opened_at_source": "webhook",
                    "approved_at": pr.approved,
                    "merged_at": pr.merged,
                    "origin": "preloop_flow" if pr.closes else "human",
                    "state": pr.state,
                    "title": pr.title,
                    "body": f"Closes #{pr.closes}" if pr.closes else "Chore.",
                    "author_login": "warp-core-bot" if pr.closes else "scotty-demo",
                    "files_changed": self.rng.randint(1, 14),
                    "head_sha": hashlib.sha1(f"pr{pr.number}".encode()).hexdigest(),
                }
            )
            if pr.closes:
                rows["pull_request_issue"].append(
                    {
                        "id": sid("pr-issue", pr.number),
                        "account_id": ACCOUNT_ID,
                        "pull_request_id": sid("pr", pr.number),
                        "issue_id": self.issue_ids[pr.closes],
                    }
                )
        for item in self.issues:
            linked = [p for p in self.prs if p.closes == item.number]
            rows["rollup"].append(
                {
                    "id": sid("rollup", item.number),
                    "account_id": ACCOUNT_ID,
                    "tracker_id": self.tracker_id,
                    "issue_key": _issue_key(item.number),
                    "issue_id": self.issue_ids[item.number],
                    "project_id": self.project_id,
                    "title": item.title,
                    "issue_url": _issue_url(item.number),
                    "pr_url": _pr_url(linked[0].number) if linked else None,
                    "first_event_at": item.created,
                }
            )

    # --- activity --------------------------------------------------------

    def _execution(
        self,
        flow: FlowSpec,
        at: datetime,
        *,
        issue: Optional[int] = None,
        pr: Optional[WorkItem] = None,
        status: Optional[str] = None,
    ) -> None:
        """One flow execution with its runtime session, usage and work facts."""
        n = len(self.data.rows["execution"])
        execution_id = sid("execution", n)
        session_id = sid("execution-session", n)
        flow_id = self.flow_ids[flow.key]
        user = (
            "scotty" if flow.key == "deploy" else self.rng.choice(("uhura", "scotty"))
        )
        status = status or ("FAILED" if self.rng.random() < 0.08 else "SUCCEEDED")
        runner = self.rng.choice(("hosted", "hosted", "uhura", "scotty"))
        calls = self.rng.randint(6, 20)
        clock = at
        tokens = 0
        cost = 0.0
        for _ in range(calls):
            clock += timedelta(seconds=self.rng.uniform(5, 90))
            t, c = self._usage(
                at=clock,
                alias=flow.model,
                user=user,
                principal=("flow_execution", str(execution_id), flow.name),
                session_id=session_id,
                auth_subject="flows",
                budget_subjects=(("flows", flow_id), ("user", self.users[user])),
                flow_id=flow_id,
                execution_id=execution_id,
                big=flow.key in {"implement", "repair"},
            )
            tokens += t
            cost += c
            if self.rng.random() < 0.5:
                tool = self.rng.choice(NATIVE_TOOLS)
                self._activity(
                    session_id=session_id,
                    at=clock,
                    activity_type="tool_call",
                    status="succeeded",
                    tool=tool,
                    summary=f"{tool} in {REPO}",
                    execution_id=execution_id,
                )
        if pr is not None:
            self._work_call(
                session_id=session_id,
                at=clock,
                tool="create_pull_request_review"
                if flow.key == "review"
                else "update_pull_request",
                args={
                    "owner": OWNER,
                    "repo": REPO,
                    "pullNumber": pr.number,
                    "event": "APPROVE" if pr.approved else "COMMENT",
                },
                execution_id=execution_id,
            )
        end = clock + timedelta(seconds=self.rng.uniform(10, 120))
        if flow.key in LOGGED_FLOWS:
            self.data.rows["execution_log"].extend(
                execution_log_rows(
                    execution_id,
                    flow=flow.key,
                    start=at,
                    end=end,
                    status=status,
                    issue=issue,
                    pr=pr.number if pr else None,
                )
            )
        self.data.rows["execution"].append(
            {
                "id": execution_id,
                "flow_id": flow_id,
                "status": status,
                "start_time": at,
                "end_time": end,
                "trigger_event_id": f"demo-event-{n}",
                "trigger_event_details": {
                    "source": "github",
                    "type": "pull_request.opened" if pr else "issues.opened",
                    "repository": REPO_PATH,
                },
                "model_output_summary": f"{flow.name} finished ({status.lower()}).",
                "runner_id": self.runner_ids[runner],
                "routing_reason": "private_runner" if runner != "hosted" else "hosted",
                "billing_mode": "gateway",
                "tool_calls_count": calls // 2,
                "total_tokens": tokens,
                "estimated_cost": round(cost, 4),
                "failure_category": "agent_error" if status == "FAILED" else None,
                "result": {"pr_url": _pr_url(pr.number)} if pr else {},
            }
        )
        self.data.rows["session"].append(
            {
                "id": session_id,
                "account_id": ACCOUNT_ID,
                "session_source_type": "flow_execution",
                "session_source_id": str(execution_id),
                "runtime_principal_type": "flow_execution",
                "runtime_principal_id": str(execution_id),
                "runtime_principal_name": flow.name,
                "title": (
                    f"{flow.name}: "
                    + (pr.title if pr else f"#{issue}" if issue else "main")
                )[:255],
                "cwd": f"/workspace/{REPO}",
                "started_at": at,
                "last_activity_at": end,
                "ended_at": end,
            }
        )
        if flow.link is None and pr is None and issue is None:
            return
        link = flow.link or "pull_request"
        rollup_issue = issue if issue is not None else (pr.closes if pr else None)
        self.data.rows["fact"].append(
            {
                "id": sid("fact", n),
                "account_id": ACCOUNT_ID,
                "execution_id": execution_id,
                "flow_id": flow_id,
                "rollup_id": sid("rollup", rollup_issue) if rollup_issue else None,
                "project_id": self.project_id,
                "pr_key": _pr_url(pr.number) if pr else None,
                "link": link,
                "status": status,
                "failure_category": "agent_error" if status == "FAILED" else None,
                "total_tokens": tokens,
                "estimated_cost": round(cost, 6),
                "start_time": at,
                "end_time": end,
            }
        )

    def flow_activity(self) -> None:
        """Lifecycle executions per work item plus scaled background runs."""
        flows = {f.key: f for f in FLOWS}
        for item in self.issues:
            self._execution(
                flows["triage"], item.created + timedelta(minutes=3), issue=item.number
            )
        for pr in self.prs:
            if pr.closes:
                key = "repair" if pr.repair else "implement"
                self._execution(flows[key], pr.created - timedelta(minutes=40), pr=pr)
            for k in range(self.rng.randint(1, 2)):
                self._execution(
                    flows["review"], pr.created + timedelta(minutes=20 + 90 * k), pr=pr
                )
            if pr.state == "open" and pr.closes and self.rng.random() < 0.5:
                self._execution(
                    flows["repair"],
                    pr.created + timedelta(hours=3),
                    pr=pr,
                    status="FAILED",
                )
            if pr.merged:
                self._execution(flows["deploy"], pr.merged + timedelta(minutes=10))
        for _ in range(self.days * 4 * self.scale):
            at = self._when(self.start)
            flow = self.rng.choice(FLOWS)
            candidates = [p for p in self.prs if p.created <= at]
            if flow.key in {"review", "repair", "implement"} and candidates:
                pr = self.rng.choice(candidates)
                self._execution(flow, at, pr=pr)
            elif flow.key == "triage":
                opened = [i for i in self.issues if i.created <= at] or self.issues
                self._execution(flow, at, issue=self.rng.choice(opened).number)
            else:
                self._execution(flows["deploy"], at)

    def agent_activity(self) -> None:
        """Agentic sessions with tool, work_ref, approval and model activity."""
        for n in range(self.days * 4 * self.scale):
            spec = self.rng.choice(AGENTS)
            agent_id = self.agent_ids[spec.key]
            at = self._when(self.start)
            session_id = sid("agent-session", n)
            open_prs = [p for p in self.prs if p.created <= at]
            open_issues = [i for i in self.issues if i.created <= at] or self.issues
            issue = self.rng.choice(open_issues)
            clock = at
            for _ in range(self.rng.randint(15, 50)):
                clock += timedelta(seconds=self.rng.uniform(4, 120))
                alias = self.rng.choice(spec.models)
                self._usage(
                    at=clock,
                    alias=alias,
                    user=spec.owner,
                    principal=("managed_agent", str(agent_id), spec.display_name),
                    session_id=session_id,
                    auth_subject="managed_agents",
                    budget_subjects=(
                        ("managed_agent", agent_id),
                        ("user", self.users[spec.owner]),
                    ),
                )
                roll = self.rng.random()
                if roll < 0.25:
                    self._activity(
                        session_id=session_id,
                        at=clock,
                        activity_type="model_gateway_call",
                        status="succeeded",
                        summary=f"{alias} call",
                        metadata={"model": alias, "provider": PRICES[alias].provider},
                    )
                elif roll < 0.7:
                    tool = self.rng.choice(NATIVE_TOOLS)
                    self._activity(
                        session_id=session_id,
                        at=clock,
                        activity_type="tool_call",
                        status="succeeded" if self.rng.random() > 0.04 else "failed",
                        tool=tool,
                        summary=f"{tool} in {REPO}",
                    )
            clock += timedelta(seconds=30)
            self._work_call(
                session_id=session_id,
                at=clock,
                tool="add_issue_comment",
                args={
                    "owner": OWNER,
                    "repo": REPO,
                    "issue_number": issue.number,
                    "body": "Investigating.",
                },
            )
            if open_prs and self.rng.random() < 0.6:
                pr = self.rng.choice(open_prs)
                clock += timedelta(seconds=40)
                if self.rng.random() < 0.3:
                    self._work_call(
                        session_id=session_id,
                        at=clock,
                        tool="create_pull_request",
                        args={
                            "owner": OWNER,
                            "repo": REPO,
                            "title": pr.title,
                            "body": f"Closes #{pr.closes}. See #{issue.number}."
                            if pr.closes
                            else f"Related to #{issue.number}.",
                        },
                    )
                self._work_call(
                    session_id=session_id,
                    at=clock + timedelta(seconds=5),
                    tool="update_pull_request",
                    args={"owner": OWNER, "repo": REPO, "pullNumber": pr.number},
                )
                if pr.merged and self.rng.random() < 0.3:
                    approved = self.rng.random() < 0.85
                    self._work_call(
                        session_id=session_id,
                        at=clock + timedelta(seconds=60),
                        tool="merge_pull_request",
                        args={"owner": OWNER, "repo": REPO, "pullNumber": pr.number},
                        status="approved" if approved else "denied",
                        extra={
                            "approval": {
                                "policy": "Merging to main needs a human",
                                "decision": "approved" if approved else "denied",
                                "approver_user_id": str(self.users["scotty"]),
                            }
                        },
                    )
            self.data.rows["session"].append(
                {
                    "id": session_id,
                    "account_id": ACCOUNT_ID,
                    "session_source_type": spec.kind,
                    # Per-run ids extend the agent's base source id, which is
                    # how the console resolves a session to its owner.
                    "session_source_id": (
                        f"demo-{spec.key}-{MEMBER_HOSTS[spec.owner]}:session-{n}"
                    ),
                    "runtime_principal_type": "managed_agent",
                    "runtime_principal_id": str(agent_id),
                    "runtime_principal_name": spec.display_name,
                    "title": f"Work on #{issue.number}: {issue.title}"[:255],
                    "cwd": f"/home/{spec.owner}/src/{REPO}",
                    "started_at": at,
                    "last_activity_at": clock,
                    "ended_at": clock
                    if clock < self.now - timedelta(minutes=30)
                    else None,
                }
            )

    # --- budgets ---------------------------------------------------------

    def budget_rows(self) -> None:
        """Three policies sized from real seeded spend: healthy, near, over."""
        spend = self.data.spend
        month = get_period_start(self.now, BudgetPeriod.monthly)
        week = get_period_start(self.now, BudgetPeriod.weekly)
        assert month is not None and week is not None
        account_month = spend.get(("account", None, BudgetPeriod.monthly, month), 0.0)
        claude_week = spend.get(
            ("managed_agent", self.agent_ids["claude_code"], BudgetPeriod.weekly, week),
            0.0,
        )
        codex_month = spend.get(
            ("managed_agent", self.agent_ids["codex"], BudgetPeriod.monthly, month), 0.0
        )
        policies = (
            ("account", None, BudgetPeriod.monthly, max(account_month * 3, 50.0), 0.8),
            (
                "managed_agent",
                self.agent_ids["claude_code"],
                BudgetPeriod.weekly,
                max(claude_week / 0.9, 0.01),
                0.75,
            ),
            (
                "managed_agent",
                self.agent_ids["codex"],
                BudgetPeriod.monthly,
                max(codex_month * 0.8, 0.01),
                0.8,
            ),
        )
        for i, (subject_type, subject_id, period, hard, soft) in enumerate(policies):
            self.data.rows["budget"].append(
                {
                    "id": sid("budget", i),
                    "account_id": ACCOUNT_ID,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "model_alias": None,
                    "period": period,
                    "hard_limit_usd": round(hard, 2),
                    "soft_limit_usd": round(hard * soft, 2),
                    "notify_on_soft": True,
                    "notify_on_hard": True,
                    "notification_user_ids": [self.users["uhura"]],
                }
            )
        for (subject_type, subject_id, period, start), usd in spend.items():
            self.data.rows["spend"].append(
                {
                    "id": sid("spend", subject_type, subject_id, period.value, start),
                    "account_id": ACCOUNT_ID,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "model_alias": "",
                    "period": period,
                    "period_start": start,
                    "spend_usd": round(usd, 6),
                }
            )

    # --- write -----------------------------------------------------------

    def create_people(self, password: str) -> None:
        """Account, the owner login (Uhura) and Scott."""
        crud_account.create(
            self.db,
            obj_in={
                "id": ACCOUNT_ID,
                "organization_name": ACCOUNT_NAME,
                "is_active": True,
                "email_verified": True,
                "meta_data": {"demo_seed": True},
            },
            commit=False,
        )
        for m in MEMBERS:
            owner = m.key == "uhura"
            user = crud_user.create(
                self.db,
                obj_in={
                    "id": sid("user", m.key),
                    "account_id": ACCOUNT_ID,
                    "username": f"{m.key}-demo",
                    "email": m.email,
                    "full_name": m.full_name,
                    "hashed_password": get_password_hash(password) if owner else None,
                    "email_verified": True,
                    "is_active": True,
                    "user_source": "local",
                },
                commit=False,
            )
            self.users[m.key] = user.id
        account = crud_account.get(self.db, ACCOUNT_ID)
        assert account is not None
        # CRUDAccount.update always commits; keep the seed in one transaction.
        CRUDBase.update(
            crud_account,
            self.db,
            db_obj=account,
            obj_in={"primary_user_id": self.users["uhura"]},
            commit=False,
        )
        for key, role_name in (("uhura", "owner"), ("scotty", "admin")):
            role = crud_role.get_by_name(self.db, name=role_name)
            if role is not None:
                crud_user_role.create(
                    self.db,
                    obj_in={"user_id": self.users[key], "role_id": role.id},
                    commit=False,
                )

    def write(self) -> dict[str, int]:
        """Insert every row group in dependency order."""
        rows = self.data.rows
        crud_expected_device.replace(
            self.db,
            account_id=ACCOUNT_ID,
            devices=[
                {
                    "device_ref_hash": _fingerprint(m.host),
                    "platform": m.platform,
                    "attributes": {
                        "hostname": m.host,
                        "owner_email": m.email,
                        "os": m.os,
                        "arch": m.arch,
                    },
                    "last_reported_at": self.now - timedelta(minutes=5),
                }
                for m in MEMBERS
            ],
        )
        rows["tracker"].append(
            {
                "id": self.tracker_id,
                "account_id": ACCOUNT_ID,
                "name": f"GitHub {OWNER}",
                "tracker_type": "github",
                "url": GITHUB,
                "is_active": True,
                "is_valid": True,
                "meta_data": {"demo_seed": True},
            }
        )
        rows["organization"].append(
            {
                "id": sid("organization"),
                "name": OWNER,
                "identifier": OWNER,
                "tracker_id": self.tracker_id,
            }
        )
        rows["project"].append(
            {
                "id": self.project_id,
                "name": REPO,
                "identifier": REPO_PATH,
                "slug": REPO_PATH,
                "organization_id": sid("organization"),
            }
        )
        plan: tuple[tuple[str, CRUDBase[Any]], ...] = (
            ("tracker", crud_tracker),
            ("organization", crud_organization),
            ("project", crud_project),
            ("ai_model", crud_ai_model),
            ("runner", crud_flow_runner),
            ("agent", crud_managed_agent),
            ("flow", crud_flow),
            ("issue", crud_issue),
            ("execution", crud_flow_execution),
            ("execution_log", crud_flow_execution_log),
            ("session", crud_runtime_session),
            ("activity", crud_runtime_session_activity),
            ("api_usage", crud_api_usage),
            ("budget", crud_budget_policy),
            ("spend", crud_budget_spend),
        )
        counts: dict[str, int] = {}
        for key, crud in plan:
            model = crud.model
            counts[key] = crud.create_many(
                self.db, rows=[_fit(model, row) for row in rows[key]]
            )
        from preloop.models import models

        for key, model in (
            ("rollup", models.IssueCostRollup),
            ("pull_request", models.IssueCostPullRequest),
            ("pull_request_issue", models.IssueCostPullRequestIssue),
            ("fact", models.IssueCostExecution),
        ):
            counts[key] = crud_issue_cost.create_many(
                self.db, model=model, rows=[_fit(model, row) for row in rows[key]]
            )
        for row in rows["rollup"]:
            crud_issue_cost.recompute_rollup(self.db, rollup_id=row["id"])
        latest: dict[str, uuid.UUID] = {}
        for row in rows["session"]:
            if row["runtime_principal_type"] == "managed_agent":
                latest[row["runtime_principal_id"]] = row["id"]
        for agent_id, session_id in latest.items():
            agent = crud_managed_agent.get(self.db, uuid.UUID(agent_id))
            if agent is not None:
                crud_managed_agent.update(
                    self.db,
                    db_obj=agent,
                    obj_in={"runtime_session_id": session_id},
                    commit=False,
                )
        counts["expected_device"] = len(MEMBERS)
        counts["user"] = len(MEMBERS)
        return counts

    def build(self) -> None:
        """Generate every row in memory (users must exist first)."""
        self.plan_work_items()
        self.static_rows()
        self.flow_activity()
        self.agent_activity()
        self.budget_rows()


MEMBER_HOSTS = {m.key: m.host for m in MEMBERS}
RUNNER_IDS = (sid("runner", "hosted"), *(sid("runner", m.key) for m in MEMBERS))
AGENT_IDS = tuple(sid("agent", a.key) for a in AGENTS)

#: Flows whose runs get a readable execution log and timeline.
LOGGED_FLOWS = frozenset({"triage", "implement"})

#: Harnesses each runner reports: (harness id, display name, version, billing).
RUNNER_HARNESSES: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "warp-ci-hosted": (
        ("claude_code", "Claude Code", "2.1.212", "metered"),
        ("codex_cli", "Codex CLI", "0.64.0", "metered"),
    ),
    "uhura-personal": (
        ("claude_code", "Claude Code", "2.1.212", "seat"),
        ("copilot_cli", "Copilot CLI", "0.0.358", "seat"),
    ),
    "scotty-personal": (
        ("codex_cli", "Codex CLI", "0.64.0", "seat"),
        ("claude_code", "Claude Code", "2.1.209", "metered"),
    ),
}


def harness_inventory(runner_name: str, now: datetime) -> dict[str, Any]:
    """The harness inventory a runner would publish with its heartbeat.

    Args:
        runner_name: Seeded runner name (a key of ``RUNNER_HARNESSES``).
        now: Report time.

    Returns:
        A ``HarnessInventory`` wire dictionary.
    """
    entries = [
        {
            "harness": harness,
            "display_name": name,
            "version": version,
            "login_state": "signed_in",
            "login_source": "stored",
            "governance": "governed",
            "support_level": "flows_and_sessions",
            "enabled": True,
            "sessions_enabled": True,
            "session_mode": "resume",
            "billing": billing,
            "models": [],
        }
        for harness, name, version, billing in RUNNER_HARNESSES.get(runner_name, ())
    ]
    digest = hashlib.sha256(repr(entries).encode()).hexdigest()
    return {
        "schema": 1,
        "generated_at": now.isoformat(),
        "hash": f"sha256:{digest}",
        "entries": entries,
    }


def execution_log_rows(
    execution_id: uuid.UUID,
    *,
    flow: str,
    start: datetime,
    end: datetime,
    status: str,
    issue: Optional[int],
    pr: Optional[int],
) -> list[dict[str, Any]]:
    """Twelve plausible log steps for one triage or implement run.

    Rows use the stored shape of streamed execution messages: ``log_type`` is
    the message type and ``metadata`` its payload.

    Args:
        execution_id: The run.
        flow: ``triage`` or ``implement``.
        start: Run start.
        end: Run end.
        status: Final status.
        issue: Issue number the run worked on, if any.
        pr: Pull request number, if any.

    Returns:
        Rows for ``flow_execution_log``, oldest first.
    """
    ref = f"{REPO_PATH}#{issue or pr or 0}"
    if flow == "triage":
        steps: list[tuple[str, dict[str, Any]]] = [
            ("status_update", {"status": "STARTING"}),
            ("agent_started", {"message": "Agent started on the hosted runner"}),
            ("agent_log_line", {"line": f"Reading {ref}", "stream": "stdout"}),
            (
                "tool_call",
                {
                    "tool_name": "get_issue",
                    "server_name": "github",
                    "status": "succeeded",
                },
            ),
            (
                "agent_log_line",
                {
                    "line": "Searching for duplicates and related issues",
                    "stream": "stdout",
                },
            ),
            (
                "tool_call",
                {
                    "tool_name": "search_issues",
                    "server_name": "github",
                    "status": "succeeded",
                },
            ),
            (
                "agent_log_line",
                {"line": "No duplicate found; 2 related issues", "stream": "stdout"},
            ),
            (
                "agent_log_line",
                {
                    "line": "Classified as bug, severity medium, area telemetry",
                    "stream": "stdout",
                },
            ),
            (
                "tool_call",
                {
                    "tool_name": "update_issue",
                    "server_name": "preloop-mcp",
                    "status": "succeeded",
                },
            ),
            (
                "tool_call",
                {
                    "tool_name": "add_comment",
                    "server_name": "preloop-mcp",
                    "status": "succeeded",
                },
            ),
            ("agent_stopped", {"message": "Agent finished"}),
        ]
    else:
        steps = [
            ("status_update", {"status": "STARTING"}),
            ("agent_started", {"message": "Agent started; workspace cloned"}),
            (
                "agent_log_line",
                {"line": f"Planning a fix for {ref}", "stream": "stdout"},
            ),
            (
                "tool_call",
                {"tool_name": "Read", "server_name": "native", "status": "succeeded"},
            ),
            (
                "tool_call",
                {"tool_name": "Grep", "server_name": "native", "status": "succeeded"},
            ),
            (
                "agent_log_line",
                {
                    "line": "Root cause: retry loop ignores the backoff ceiling",
                    "stream": "stdout",
                },
            ),
            (
                "tool_call",
                {"tool_name": "Edit", "server_name": "native", "status": "succeeded"},
            ),
            (
                "tool_call",
                {"tool_name": "Bash", "server_name": "native", "status": "succeeded"},
            ),
            (
                "agent_log_line",
                {"line": "pytest: 214 passed in 41.2s", "stream": "stdout"},
            ),
            (
                "agent_log_line",
                {"line": "Committed 3 files; pushing branch", "stream": "stdout"},
            ),
            (
                "tool_call",
                {
                    "tool_name": "create_pull_request",
                    "server_name": "preloop-mcp",
                    "status": "succeeded",
                },
            ),
            ("agent_stopped", {"message": "Agent finished"}),
        ]
    steps.append(("status_update", {"status": status}))
    span = (end - start) / len(steps)
    return [
        {
            "id": sid("execution-log", f"{execution_id}:{index}"),
            "execution_id": execution_id,
            "timestamp": start + span * index,
            "log_type": kind,
            "message": payload.get("line") or payload.get("message"),
            "metadata_": payload,
        }
        for index, (kind, payload) in enumerate(steps)
    ]


def keep_alive(db: Session, *, interval: float, rounds: Optional[int] = None) -> int:
    """Refresh the demo runners' heartbeats and agents' last-seen times.

    Runner presence turns offline 45 s after the last heartbeat, so a static
    seed cannot stay "Online" for a recording. This loop does what a live
    runner would, until interrupted (or for ``rounds`` passes).

    Args:
        db: Database session.
        interval: Seconds between passes.
        rounds: Passes to run; None runs until Ctrl-C.

    Returns:
        Passes completed.
    """
    done = 0
    while rounds is None or done < rounds:
        now = datetime.now(UTC)
        for runner_id in RUNNER_IDS:
            runner = crud_flow_runner.get(db, runner_id)
            if runner is not None:
                crud_flow_runner.update(
                    db,
                    db_obj=runner,
                    obj_in={"last_heartbeat": now, "status": "online"},
                    commit=False,
                )
        for agent_id in AGENT_IDS:
            agent = crud_managed_agent.get(db, agent_id)
            if agent is not None:
                crud_managed_agent.update(
                    db,
                    db_obj=agent,
                    obj_in={"last_seen_at": now.replace(tzinfo=None)},
                    commit=False,
                )
        db.commit()
        done += 1
        if rounds is None or done < rounds:
            time.sleep(interval)
    return done


def remove_demo_account(db: Session) -> bool:
    """Delete the demo account and everything it owns, without committing.

    Gateway usage only nulls its account on delete, and flows, flow
    executions and AI models block it, so those go first; the database
    cascades the rest. Seeded identities without a membership go last.

    Returns:
        True when a demo account existed.
    """
    if crud_account.get(db, ACCOUNT_ID) is None:
        return False
    flow_ids = [
        f.id for f in crud_flow.get_multi(db, limit=1000, account_id=str(ACCOUNT_ID))
    ]
    crud_api_usage.delete_where_in(db, column="account_id", values=[ACCOUNT_ID])
    crud_flow_execution.delete_where_in(db, column="flow_id", values=flow_ids)
    crud_flow.delete_where_in(db, column="id", values=flow_ids)
    crud_ai_model.delete_where_in(db, column="account_id", values=[ACCOUNT_ID])
    crud_account.delete_where_in(db, column="id", values=[ACCOUNT_ID])
    crud_person.delete_unattached(db, emails=[m.email for m in MEMBERS])
    return True


@click.command()
@click.option("--database-url", envvar="DATABASE_URL", help="Defaults to DATABASE_URL.")
@click.option("--days", default=28, show_default=True, type=click.IntRange(3, 365))
@click.option(
    "--scale",
    default=1,
    show_default=True,
    type=click.IntRange(1, 100),
    help="Activity multiplier: 1 is about 5-10k usage rows, 10 about 50-100k.",
)
@click.option("--seed", default=1701, show_default=True, type=int)
@click.option("--reset", is_flag=True, help="Only remove the demo account's data.")
@click.option(
    "--keepalive",
    is_flag=True,
    help="After seeding (or alone with --no-seed), keep runners and agents "
    "online by refreshing heartbeats every 15 s until Ctrl-C.",
)
@click.option("--no-seed", is_flag=True, help="Skip seeding; use with --keepalive.")
@click.option(
    "--password",
    envvar="DEMO_TEAM_PASSWORD",
    help="Owner login password (or DEMO_TEAM_PASSWORD).",
)
def seed_demo_team(
    database_url: Optional[str],
    days: int,
    scale: int,
    seed: int,
    reset: bool,
    keepalive: bool,
    no_seed: bool,
    password: Optional[str],
) -> None:
    """Seed (or with --reset, remove) the synthetic Enterprise Labs demo team."""
    load_dotenv()
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise click.UsageError("Pass --database-url or set DATABASE_URL.")
    if no_seed and not keepalive:
        raise click.UsageError("--no-seed only makes sense with --keepalive.")
    if not reset and not no_seed and not password:
        raise click.UsageError("Pass --password or set DEMO_TEAM_PASSWORD.")
    # The session factory reads DATABASE_URL, so --database-url must win there too.
    os.environ["DATABASE_URL"] = url
    setup_database(url)
    db = next(get_db_session())
    if no_seed:
        _run_keepalive(db)
        return
    started = time.monotonic()
    try:
        removed = remove_demo_account(db)
        if reset:
            db.commit()
            click.echo("Demo account removed." if removed else "No demo account found.")
            return
        assert password is not None
        seeder = Seeder(db, days=days, scale=scale, seed=seed)
        seeder.create_people(password)
        seeder.build()
        counts = seeder.write()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    click.echo(f"{'Replaced' if removed else 'Created'} demo account {ACCOUNT_ID}.")
    click.echo(f"Owner login: {MEMBERS[0].email} (password set, not shown)")
    for key in sorted(counts):
        click.echo(f"  {key:<20} {counts[key]:>8}")
    click.echo(f"Done in {time.monotonic() - started:.1f}s.")
    if keepalive:
        _run_keepalive(next(get_db_session()))


def _run_keepalive(db: Session) -> None:
    click.echo("Keeping runners and agents online; Ctrl-C to stop.")
    try:
        keep_alive(db, interval=15.0)
    except KeyboardInterrupt:
        click.echo("Stopped.")
    finally:
        db.close()


if __name__ == "__main__":
    seed_demo_team()

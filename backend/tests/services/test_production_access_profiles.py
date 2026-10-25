"""The read-only production access example refuses writes and allows reads.

Runs tool calls through the policy simulator, which uses the production
matchers, and checks that the credential and server files agree with the
policy (#1426).
"""

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import yaml

from preloop.api.endpoints.tools import BUILTIN_TOOLS
from preloop.services.policy import load_policy_from_string
from preloop.services.policy_simulation import (
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    simulate_policy,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "docs/guide/examples/production-access"
POLICY = (EXAMPLE / "readonly-policy.yaml").read_text()
SERVERS = ["k8s-readonly", "postgres-readonly", "sentry-readonly"]
K8S = "k8s-readonly-allowlist"
PG = "postgres-readonly-allowlist"
SQL = "postgres-readonly-sql-guard"
SENTRY = "sentry-readonly-allowlist"


async def decide(
    server: str, name: str, args: dict[str, Any], policy: str = POLICY
) -> PolicyEvaluationResponse:
    """Evaluate one tool call against a draft policy without a database."""
    request = PolicyEvaluationRequest.model_validate(
        {"name": name, "server": server, "args": args, "draft_yaml": policy}
    )
    return await simulate_policy(
        request,
        db=MagicMock(),
        account_id=uuid4(),
        user_id=uuid4(),
    )


def test_policy_shape() -> None:
    policy, validation = load_policy_from_string(POLICY)
    assert validation.is_valid, validation.errors
    assert policy is not None
    # defaults cannot deny unknown tools; the allowlist rules do that.
    assert policy.defaults is None
    assert {tool.source for tool in policy.tools or []} <= set(SERVERS)
    assert policy.sensitive_data is not None
    rules = policy.sensitive_data.rules
    assert [rule.id for rule in rules] == [K8S, PG, SQL, SENTRY]
    for rule in rules:
        assert rule.action_value() == "deny"
        assert rule.target_values() == ["tool.args"]
        assert rule.scope.servers and set(rule.scope.servers) <= set(SERVERS)
        assert rule.condition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server,name,args,decision,rule",
    [
        (
            "k8s-readonly",
            "pods_log",
            {"name": "web-0", "namespace": "shop"},
            "allow",
            None,
        ),
        ("k8s-readonly", "resources_create_or_update", {"resource": "{}"}, "deny", K8S),
        ("k8s-readonly", "resources_delete", {"kind": "Deployment"}, "deny", K8S),
        ("k8s-readonly", "resources_scale", {"scale": 0}, "deny", K8S),
        ("k8s-readonly", "pods_delete", {"name": "web-0"}, "deny", K8S),
        ("k8s-readonly", "pods_exec", {"command": ["sh"]}, "deny", K8S),
        ("k8s-readonly", "pods_run", {"image": "busybox"}, "deny", K8S),
        ("k8s-readonly", "configuration_view", {}, "deny", K8S),
        ("k8s-readonly", "nodes_log", {"name": "node-1"}, "deny", K8S),
        ("k8s-readonly", "helm_install", {"chart": "example"}, "deny", K8S),
        # A tool a later upstream release adds is denied, not allowed by default.
        ("k8s-readonly", "brand_new_write_tool", {}, "deny", K8S),
        ("K8S-READONLY", "brand_new_write_tool", {}, "deny", K8S),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT id FROM orders"},
            "allow",
            None,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "select update_time, deleted_at from orders"},
            "allow",
            None,
        ),
        ("postgres-readonly", "search_objects", {"pattern": "orders"}, "allow", None),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "INSERT INTO t VALUES (1)"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "update t set a = 1"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "DELETE FROM orders"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "MERGE INTO t USING s ON true"},
            "deny",
            SQL,
        ),
        ("postgres-readonly", "execute_sql", {"sql": "DROP TABLE orders"}, "deny", SQL),
        ("postgres-readonly", "execute_sql", {"sql": "TRUNCATE orders"}, "deny", SQL),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "ALTER ROLE x SUPERUSER"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "CREATE TABLE t (a int)"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "GRANT ALL ON t TO x"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "COPY orders TO PROGRAM 'sh'"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SET default_transaction_read_only = off"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT set_config('statement_timeout', '0', false)"},
            "deny",
            SQL,
        ),
        # Statements that switch the session to read-write.
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "BEGIN READ WRITE; SELECT * INTO TEMP x FROM orders; COMMIT"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "start  transaction read write"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT * INTO new_table FROM orders"},
            "deny",
            SQL,
        ),
        # A U& identifier can spell set_config without the keyword.
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT U&\"s\\0065t_config\"('statement_timeout', '0', false)"},
            "deny",
            SQL,
        ),
        ("postgres-readonly", "execute_sql", {"sql": "NOTIFY jobs, 'x'"}, "deny", SQL),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT pg_notify('jobs', 'x')"},
            "deny",
            SQL,
        ),
        ("postgres-readonly", "execute_sql", {"sql": "LISTEN jobs"}, "deny", SQL),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT pg_advisory_lock(42)"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT pg_try_advisory_xact_lock(42)"},
            "deny",
            SQL,
        ),
        # Large objects persist and need no table privilege.
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT lo_from_bytea(0, 'x')"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT lo_create(0)"},
            "deny",
            SQL,
        ),
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT lo_put(16399, 0, 'x')"},
            "deny",
            SQL,
        ),
        # Column names that contain a keyword are still allowed.
        (
            "postgres-readonly",
            "execute_sql",
            {"sql": "SELECT started_at, begin_date, lo_value FROM jobs WHERE done"},
            "allow",
            None,
        ),
        # Missing or non-string SQL fails closed.
        ("postgres-readonly", "execute_sql", {}, "deny", SQL),
        ("postgres-readonly", "execute_sql", {"sql": 1}, "deny", SQL),
        ("postgres-readonly", "explain_sql", {"sql": "SELECT 1"}, "deny", PG),
        (
            "sentry-readonly",
            "sentry_search_issues",
            {"query": "is:unresolved"},
            "allow",
            None,
        ),
        (
            "sentry-readonly",
            "sentry_update_issue",
            {"status": "resolved"},
            "deny",
            SENTRY,
        ),
        ("sentry-readonly", "sentry_analyze_issue_with_seer", {}, "deny", SENTRY),
        # Registered without the prefix, the server's tools are all denied.
        (
            "sentry-readonly",
            "search_issues",
            {"query": "is:unresolved"},
            "deny",
            SENTRY,
        ),
        (
            "sentry-readonly",
            "execute_sentry_tool",
            {"name": "get_issue_details"},
            "deny",
            SENTRY,
        ),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "get_issue_details", "arguments": {"issueId": "EXAMPLE-1"}},
            "allow",
            None,
        ),
        # Sentry receives the name unprefixed, so a prefixed name is not a read.
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "sentry_get_issue_details"},
            "deny",
            SENTRY,
        ),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "update_issue"},
            "deny",
            SENTRY,
        ),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "delete_alert_rule"},
            "deny",
            SENTRY,
        ),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "onboarding_status_update"},
            "deny",
            SENTRY,
        ),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": " update_issue "},
            "deny",
            SENTRY,
        ),
        # A nested call would hide the inner name, so the executor is not a read.
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "execute_sentry_tool", "arguments": {"name": "update_issue"}},
            "deny",
            SENTRY,
        ),
        ("sentry-readonly", "sentry_execute_sentry_tool", {}, "deny", SENTRY),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": ["find_projects"]},
            "deny",
            SENTRY,
        ),
        # Other servers are not affected by this profile.
        ("preloop-mcp", "ask_user", {}, "allow", None),
        ("crm", "delete_record", {"id": "record-001"}, "allow", None),
    ],
)
async def test_tool_decisions(
    server: str, name: str, args: dict[str, Any], decision: str, rule: str | None
) -> None:
    result = await decide(server, name, args)
    assert result.decision == decision
    assert result.matched_rule == rule
    if decision == "deny":
        assert result.description and "Read-only profile" in result.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server,name,args,rule",
    [
        ("k8s-readonly", "future_write_tool", {}, K8S),
        ("postgres-readonly", "future_write_tool", {}, PG),
        ("sentry-readonly", "sentry_future_write_tool", {}, SENTRY),
        (
            "sentry-readonly",
            "sentry_execute_sentry_tool",
            {"name": "future_write_tool"},
            SENTRY,
        ),
    ],
)
async def test_tool_missing_from_the_policy_is_denied(
    server: str, name: str, args: dict[str, Any], rule: str
) -> None:
    """A tool the policy never names, such as one a later release adds, is denied."""
    assert "future_write_tool" not in POLICY
    result = await decide(server, name, args)
    assert result.decision == "deny"
    assert result.matched_rule == rule
    assert result.description and "Read-only profile" in result.description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "server,name",
    [
        ("k8s-readonly", "resources_create_or_update"),
        ("k8s-readonly", "resources_delete"),
        ("k8s-readonly", "resources_scale"),
        ("k8s-readonly", "pods_delete"),
        ("k8s-readonly", "pods_exec"),
        ("k8s-readonly", "pods_run"),
        ("sentry-readonly", "sentry_update_issue"),
        ("sentry-readonly", "sentry_onboarding_status_update"),
    ],
)
async def test_known_writes_denied_without_allowlist_rules(
    server: str, name: str
) -> None:
    """The per-tool entries hold if the allowlist rules are disabled."""
    document = yaml.safe_load(POLICY)
    for item in document["sensitive_data"]["rules"]:
        item["enabled"] = False
    result = await decide(server, name, {}, policy=yaml.safe_dump(document))
    assert result.decision == "deny"
    assert result.matched_rule == "draft-1"
    assert result.description and "Read-only profile" in result.description


def test_rbac_manifest_binds_view_only() -> None:
    manifest = yaml.safe_load((EXAMPLE / "k8s-readonly-rbac.yaml").read_text())
    assert manifest["kind"] == "List"
    # No Role or ClusterRole is defined here, so nothing grants beyond view.
    assert [item["kind"] for item in manifest["items"]] == [
        "Namespace",
        "ServiceAccount",
        "ClusterRoleBinding",
    ]
    binding = manifest["items"][2]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "view",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "agent-readonly",
            "namespace": "agent-access",
        }
    ]
    script = (EXAMPLE / "verify-readonly-rbac.sh").read_text()
    assert "${SA_NAMESPACE:-agent-access}:${SA_NAME:-agent-readonly}" in script


def test_postgres_role_grants_reads_only() -> None:
    lines = (EXAMPLE / "postgres-readonly-role.sql").read_text().splitlines()
    sql = "\n".join(line.split("--", 1)[0] for line in lines)
    statements = [" ".join(part.split()) for part in sql.split(";") if part.strip()]
    grants = [s for s in statements if re.search(r"(^|\s)GRANT\s", s)]
    assert len(grants) == 4
    for grant in grants:
        match = re.search(r"GRANT (.+?) ON ", grant)
        assert match, grant
        assert {p.strip() for p in match.group(1).split(",")} <= {
            "CONNECT",
            "USAGE",
            "SELECT",
        }
    create = next(s for s in statements if s.startswith("CREATE ROLE agent_readonly"))
    for attribute in [
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
    ]:
        assert attribute in create
    settings = dict(
        re.findall(r"ALTER ROLE agent_readonly SET (\w+) = '?([^';\s]+)", sql)
    )
    assert settings == {
        "default_transaction_read_only": "on",
        "statement_timeout": "15s",
        "lock_timeout": "2s",
        "idle_in_transaction_session_timeout": "30s",
    }


@pytest.mark.asyncio
async def test_kubernetes_server_config_matches_policy() -> None:
    config = tomllib.loads((EXAMPLE / "kubernetes-mcp-server.toml").read_text())
    assert config["read_only"] is True
    assert config["toolsets"] == ["core"]
    assert config["cluster_auth_mode"] == "kubeconfig"
    assert {"group": "", "version": "v1", "kind": "Secret"} in config[
        "denied_resources"
    ]
    policy, _ = load_policy_from_string(POLICY)
    assert policy is not None
    denied = {tool.name for tool in policy.tools or [] if tool.source == SERVERS[0]}
    assert denied.isdisjoint(config["enabled_tools"])
    for name in config["enabled_tools"]:
        assert (await decide("k8s-readonly", name, {})).decision == "allow", name


@pytest.mark.asyncio
async def test_dbhub_config_matches_policy() -> None:
    config = tomllib.loads((EXAMPLE / "dbhub.toml").read_text())
    # One source keeps the unsuffixed tool names the policy lists.
    [source] = config["sources"]
    assert source["dsn"].startswith("postgres://agent_readonly:${AGENT_DB_PASSWORD}@")
    assert source["query_timeout"] > 0
    # The pool must not open more connections than the role is allowed.
    role_sql = (EXAMPLE / "postgres-readonly-role.sql").read_text()
    [limit] = re.findall(r"CONNECTION LIMIT (\d+)", role_sql)
    assert source["pool_max_connections"] == int(limit)
    tools = {tool["name"]: tool for tool in config["tools"]}
    assert set(tools) == {"execute_sql", "search_objects"}
    assert all(tool["source"] == source["id"] for tool in tools.values())
    assert tools["execute_sql"]["readonly"] is True
    assert tools["execute_sql"]["max_rows"] > 0
    for name in tools:
        result = await decide("postgres-readonly", name, {"sql": "SELECT 1"})
        assert result.decision == "allow", name


@pytest.mark.asyncio
async def test_flow_snippet_tools_are_allowed() -> None:
    readme = (EXAMPLE / "README.md").read_text()
    [flow] = [
        yaml.safe_load(block)
        for block in re.findall(r"```yaml\n(.*?)```", readme, re.DOTALL)
        if "allowed_mcp_tools" in block
    ]
    assert flow["allowed_mcp_servers"] == SERVERS
    sample_args: dict[str, dict[str, Any]] = {
        "execute_sql": {"sql": "SELECT id FROM orders"},
        "sentry_execute_sentry_tool": {"name": "get_issue_details"},
    }
    builtins = {tool["name"] for tool in BUILTIN_TOOLS}
    for entry in flow["allowed_mcp_tools"]:
        assert entry["server_name"] in SERVERS
        name = entry["tool_name"]
        # An MCP tool named like a built-in takes over the built-in's calls.
        assert name not in builtins, entry
        result = await decide(entry["server_name"], name, sample_args.get(name, {}))
        assert result.decision == "allow", entry


# What `kubectl auth can-i --list` prints for a ServiceAccount bound to view
# (abridged), including the self-review and discovery rules every
# authenticated user has. The metrics.k8s.io rules come from metrics-server's
# ClusterRole, which is aggregated into view.
VIEW_RULES = """\
Resources                                       Non-Resource URLs   Resource Names   Verbs
selfsubjectaccessreviews.authorization.k8s.io   []                  []               [create]
selfsubjectreviews.authentication.k8s.io        []                  []               [create]
selfsubjectrulesreviews.authorization.k8s.io    []                  []               [create]
configmaps                                      []                  []               [get list watch]
deployments.apps                                []                  []               [get list watch]
pods                                            []                  []               [get list watch]
pods/log                                        []                  []               [get list watch]
nodes.metrics.k8s.io                            []                  []               [get list watch]
pods.metrics.k8s.io                             []                  []               [get list watch]
                                                [/api/*]            []               [get]
                                                [/version]          []               [get]
"""

# Stands in for kubectl: answers can-i questions like a ServiceAccount bound
# to view, and prints $RULES for --list (or fails when $RULES is empty).
KUBECTL_STUB = """\
#!/usr/bin/env bash
if [ "$1 $2" = "get namespaces" ]; then echo "default payments"; exit 0; fi
for a in "$@"; do
  if [ "$a" = --list ]; then
    [ -n "$RULES" ] || { echo "cannot impersonate" >&2; exit 1; }
    printf '%s' "$RULES"
    exit 0
  fi
done
case "$3" in get | list | watch) ;; *) exit 1 ;; esac
case "$4" in secrets | nodes | '*') exit 1 ;; esac
for a in "$@"; do
  case "$a" in --subresource=log) ;; --subresource=*) exit 1 ;; esac
done
exit 0
"""


def run_rbac_script(tmp_path: Path, rules: str) -> subprocess.CompletedProcess[str]:
    """Run verify-readonly-rbac.sh with the kubectl stand-in on PATH."""
    stub = tmp_path / "kubectl"
    stub.write_text(KUBECTL_STUB)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "RULES": rules,
    }
    return subprocess.run(
        [shutil.which("bash") or "bash", str(EXAMPLE / "verify-readonly-rbac.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    "extra_rule",
    [
        None,
        # Rules a chart could aggregate into view, or another binding add.
        "configmaps   []   []   [create]",
        "widgets.example.com   []   []   [get delete]",
        "persistentvolumeclaims   []   [data-0]   [patch]",
        "secrets   []   []   [get list watch]",
        "pods/exec   []   []   [get]",
        "*.*   []   []   [get]",
        "   [/metrics]   []   [get post]",
    ],
)
def test_rbac_script_reads_the_rules(tmp_path: Path, extra_rule: str | None) -> None:
    """verify-readonly-rbac.sh fails on any rule that view does not grant."""
    rules = VIEW_RULES + (f"{extra_rule}\n" if extra_rule else "")
    result = run_rbac_script(tmp_path, rules)
    output = result.stdout + result.stderr
    if extra_rule is None:
        assert result.returncode == 0, output
        assert "All checks passed" in result.stdout
    else:
        assert result.returncode == 1, output
        verbs = extra_rule.rsplit("[", 1)[1]
        # Reported once per namespace (default and payments).
        assert result.stdout.count(f"[{verbs}") == 2, output


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_rbac_script_fails_when_rules_cannot_be_read(tmp_path: Path) -> None:
    result = run_rbac_script(tmp_path, "")
    assert result.returncode == 1
    assert "cannot impersonate" in result.stdout

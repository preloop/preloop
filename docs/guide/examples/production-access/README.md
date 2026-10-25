# Read-only production access for agents

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

An incident investigation flow needs to read production: pod state and logs,
rows in the database, errors in Sentry. It should have no way to change any of
them. This example sets that up for three MCP servers and binds them to one
flow ([#1426](https://github.com/preloop/preloop/issues/1426)). Changes go
through a separate operator flow in which every action needs approval; that
profile is not part of this example yet.

The example was written against these upstream releases. Tool names and
options change between releases, so check them when you upgrade.

| MCP server | Release | Registered in Preloop as | Tool prefix |
| --- | --- | --- | --- |
| [kubernetes-mcp-server](https://github.com/containers/kubernetes-mcp-server/tree/v0.0.67) | v0.0.67 | `k8s-readonly` | none |
| [DBHub](https://github.com/bytebase/dbhub/tree/v1.5.0) for PostgreSQL | v1.5.0 | `postgres-readonly` | none |
| [Sentry MCP](https://github.com/getsentry/sentry-mcp/tree/0.42.0) | 0.42.0 | `sentry-readonly` | `sentry` |

## Four layers

| Layer | Kubernetes | PostgreSQL | Sentry | Enforced by |
| --- | --- | --- | --- | --- |
| 1. Credential | ServiceAccount bound to ClusterRole `view` | Role with `SELECT` grants only | Token with read scopes only | Kubernetes, PostgreSQL, Sentry |
| 2. MCP server settings | `read_only`, `enabled_tools`, Secrets denied | `readonly`, `max_rows`, `query_timeout` | `inspect` skill only | The MCP server |
| 3. Flow tool allowlist | Read tools | `execute_sql`, `search_objects` | Read tools and `sentry_execute_sentry_tool` | Preloop |
| 4. Policy | Every tool off the read list denied | The same, plus a SQL keyword guard | The same, including the tool that `execute_sentry_tool` runs | Preloop |

The credential is the boundary. It is the only layer that still holds when
every other one is misconfigured: the API server, the database and Sentry
refuse a write that the credential has no permission for, whatever the agent,
the MCP server or Preloop decide. PostgreSQL has two exceptions that no grant
to the role controls: by default every role can create temporary tables and
large objects. Closing them takes a database-wide change, described under
[What this does not protect against](#what-this-does-not-protect-against).
Layers 2 to 4 narrow what the agent can call. Layers 3 and 4 refuse inside
Preloop, before the call reaches the server, and the refusal is recorded
there.

No agent receives a Kubernetes credential, with or without write verbs. The
agent calls tools through Preloop; the MCP server holds the ServiceAccount
token, and that ServiceAccount can get, list and watch, nothing else. The same
holds for the database password and the Sentry token.

## Files

| File | Purpose |
| --- | --- |
| [readonly-policy.yaml](readonly-policy.yaml) | Preloop policy for the three servers |
| [k8s-readonly-rbac.yaml](k8s-readonly-rbac.yaml) | Namespace, ServiceAccount and binding to ClusterRole `view` |
| [kubernetes-mcp-server.toml](kubernetes-mcp-server.toml) | kubernetes-mcp-server settings |
| [verify-readonly-rbac.sh](verify-readonly-rbac.sh) | Checks the ServiceAccount's permissions with `kubectl auth can-i` |
| [postgres-readonly-role.sql](postgres-readonly-role.sql) | The `agent_readonly` role and its grants |
| [dbhub.toml](dbhub.toml) | DBHub settings |
| [verify-readonly-postgres.sh](verify-readonly-postgres.sh) | Connects as the role and checks it can read and cannot write |

## Why the policy is an allowlist

A tool call that no rule matches is allowed
([Subject-scoped governance](../../../architecture/governance.md)), and policy
`defaults` cannot change that: the loader refuses `unknown_tools` values other
than `allow`. A policy that only lists write tools therefore lets through any
tool a later upstream release adds.

So each server gets a `sensitive_data` rule whose CEL `condition` denies every
tool that is not on the server's read list, the mechanism the
[back-office policy pack](../../policies/back-office-ai.md) uses. These rules
run before tool access rules, and a deny rule whose condition fails to
evaluate denies the call. A new upstream tool stays denied until you add it to
the list. The `tools:` section also denies the known write tools one by one,
so the refusal shows on each tool in **Tools** and still holds if someone
disables an allowlist rule.

`execute_sentry_tool` runs another Sentry tool by name, writes included, so the
Sentry rule checks its `name` argument against the same read list.

## Set up

### Kubernetes

1. Create the identity and check it:

    ```bash
    kubectl apply -f k8s-readonly-rbac.yaml
    bash verify-readonly-rbac.sh
    ```

    The script asks the API server, as the ServiceAccount, about a fixed list
    of reads and writes. Then it reads the ServiceAccount's rules in every
    namespace (`kubectl auth can-i --list`) and fails on any verb other than
    `get`, `list` and `watch` (apart from the self-review `create` that every
    user has), and on any rule for Secrets, for all resources, or for the
    `exec`, `attach`, `portforward` and `proxy` subresources. It exits
    non-zero on any finding. `view` is an aggregated role that installed
    charts can extend, so run the script again after cluster changes. To limit
    reads to some namespaces, bind ClusterRole `view` with a RoleBinding in
    each and run the script with `NAMESPACE=<name>`.

2. Run kubernetes-mcp-server in the cluster as the `agent-readonly`
   ServiceAccount, started with `--config kubernetes-mcp-server.toml`. With
   the upstream Helm chart, set `serviceAccount.create=false`,
   `serviceAccount.name=agent-readonly` and `rbac.create=false`, so the chart
   neither creates another identity nor grants more.

    The settings expose only the read tools, never load the kubeconfig tools,
    refuse Secrets in the server as well, and set `cluster_auth_mode` to
    `kubeconfig` so the server uses its own ServiceAccount rather than forward
    a caller's `Authorization` header (the upstream default is `passthrough`).
    `nodes_log` and `nodes_stats_summary` are left out: they read through the
    kubelet proxy, which `view` does not grant. `pods_top` and `nodes_top` read
    `metrics.k8s.io`, which exists only where metrics-server runs. Its
    ClusterRole (`system:aggregated-metrics-reader` in the upstream manifest,
    its Helm chart and k3s) is aggregated into `view`, so the binding above
    covers both tools; without metrics-server they return an error.

### PostgreSQL

1. Create the role, set its password with `\password agent_readonly` in
   `psql`, and check it. Run the file as a superuser, or as a role that has
   `CREATEROLE` and is `app_owner` or a member of it; `app_owner` is the role
   that owns the database and its tables (`ALTER DEFAULT PRIVILEGES FOR ROLE`
   needs that membership).

    ```bash
    psql -1 -v ON_ERROR_STOP=1 -v app_owner=app_owner -d app_db \
      -f postgres-readonly-role.sql
    PGDATABASE=app_db PGUSER=agent_readonly bash verify-readonly-postgres.sh
    ```

    The script checks the role's attributes, privileges and role memberships
    in the catalog, probes `CREATE TABLE`, `INSERT`, `UPDATE` and `DELETE`
    (with `EXPLAIN` or a rolled-back transaction, so nothing changes even if a
    probe gets through), and waits for `statement_timeout` to cancel a long
    query. It warns about the PUBLIC defaults that let the role create
    temporary tables and large objects, and lists `SECURITY DEFINER`
    functions the role can run.

2. Run DBHub with `dbhub.toml`. It reads the role's password from an
   environment variable, refuses statements other than reads (`readonly`),
   returns at most 500 rows (`max_rows`) and times queries out after 15
   seconds (`query_timeout`). PostgreSQL has no per-role row cap, so the cap
   lives in DBHub.

### Sentry

1. Create a Sentry auth token with `org:read`, `project:read`, `team:read`
   and `event:read`, the scopes that the read tools on the list declare.
   The server's README also lists `project:write`, `team:write` and
   `event:write` for its stdio transport; those serve its write tools, which
   this profile does not use.
2. Run the Sentry MCP server with only the `inspect` skill (`--skills=inspect`,
   or `?skills=inspect` on the hosted server's URL). The hosted server takes a
   token in an `Authorization: Sentry-Bearer <token>` header. In Preloop that
   is the API's `api_key` authentication type with key name `Authorization`,
   and the API key is the whole header value, `Sentry-Bearer <token>`:
   `api_key` sends the value as it is, without adding a scheme.
   The `inspect` skill still includes `onboarding_status_update`, which changes
   onboarding state in Sentry, so the policy refuses it per tool and checks
   every tool name anyway.
3. Give the server the tool prefix `sentry` when you register it in Preloop
   (**Tool prefix** in the console, or
   `preloop mcp-servers update sentry-readonly --tool-prefix sentry`). Sentry's
   `search_issues`, and `update_issue` in its `triage` skill, have the same
   names as Preloop's built-in tools, and an MCP tool with a built-in's name
   takes over every call to that name in the account, including the calls
   that the Issue Triage preset makes. With the prefix the tools are named
   `sentry_search_issues` and so on, which is what the policy and the flow
   below use.

This example was written for Sentry and has not been tried against GlitchTip.
GlitchTip implements only part of Sentry's API: its 6.2.6 source has no
`/api/0/auth/` or organization-level `/events/` endpoint, and the Sentry MCP
server's API client calls both, so some of the tools are likely to fail there.

### Preloop

Register the three servers under the names above (see
[External MCP tools](../../tools/external-mcp.md)) and scan their tools. Then
validate, preview and apply the policy:

```bash
preloop policy validate readonly-policy.yaml
preloop policy diff readonly-policy.yaml
preloop policy apply --dry-run readonly-policy.yaml
preloop policy apply readonly-policy.yaml
```

The `tools:` entries name the servers, so register them first. The
`sensitive_data` section replaces the account's current one: if you already
have sensitive-data rules, merge them into this file before you apply it.

The policy and the flow use the Sentry tools' prefixed names, `sentry_<tool>`.
The one exception is the `name` argument of `sentry_execute_sentry_tool`:
Sentry receives it as is, so it stays the upstream name, and the Sentry rule
adds `sentry_` before it compares the name with the read list. If you choose
another prefix, change `sentry_` in the rule, the `tools:` entry and the flow.
If you give the Kubernetes or PostgreSQL server a prefix, rename their tools
in the policy and the flow the same way.

## Bind the profile to one flow

Give the investigation flow these servers and tools, and no others:

```yaml
allowed_mcp_servers:
  - k8s-readonly
  - postgres-readonly
  - sentry-readonly
allowed_mcp_tools:
  - server_name: k8s-readonly
    tool_name: resources_list
  - server_name: k8s-readonly
    tool_name: resources_get
  - server_name: k8s-readonly
    tool_name: pods_list
  - server_name: k8s-readonly
    tool_name: pods_get
  - server_name: k8s-readonly
    tool_name: pods_log
  - server_name: k8s-readonly
    tool_name: events_list
  - server_name: postgres-readonly
    tool_name: execute_sql
  - server_name: postgres-readonly
    tool_name: search_objects
  - server_name: sentry-readonly
    tool_name: sentry_search_issues
  - server_name: sentry-readonly
    tool_name: sentry_get_sentry_resource
  - server_name: sentry-readonly
    tool_name: sentry_search_sentry_tools
  - server_name: sentry-readonly
    tool_name: sentry_execute_sentry_tool
```

`allowed_mcp_tools` limits both the tools Preloop lists to the flow and the
tools it runs for the flow's key
([Agent isolation](../../../security/agent-isolation.md)). Tools are matched
by name, so keep tool names unique across the account's servers and
different from Preloop's built-in tools
([Same tool name on two servers](../../tools/external-mcp.md#same-tool-name-on-two-servers)).
A Codex flow that only investigates can also set
`agent_config.sandbox_type: read-only`, which removes its shell tool.

The policy applies to every caller of these servers, not only this flow. Give
the operator flow its own servers with their own credentials rather than
granting it these.

## Check the policy

When policy simulation is enabled, use **Simulate** in the Policies YAML editor
with the draft policy. The pytest
`backend/tests/services/test_production_access_profiles.py` runs the same
cases, and more, through the policy simulator.

| Server | Tool and arguments | Decision | Rule |
| --- | --- | --- | --- |
| `k8s-readonly` | `pods_log` | allow | |
| `k8s-readonly` | `resources_delete` | deny | `k8s-readonly-allowlist` |
| `k8s-readonly` | a tool not on the read list | deny | `k8s-readonly-allowlist` |
| `postgres-readonly` | `execute_sql`, `{"sql": "SELECT id FROM orders"}` | allow | |
| `postgres-readonly` | `execute_sql`, `{"sql": "DELETE FROM orders"}` | deny | `postgres-readonly-sql-guard` |
| `sentry-readonly` | `sentry_execute_sentry_tool`, `{"name": "get_issue_details"}` | allow | |
| `sentry-readonly` | `sentry_execute_sentry_tool`, `{"name": "update_issue"}` | deny | `sentry-readonly-allowlist` |
| `sentry-readonly` | `search_issues` (server registered without the prefix) | deny | `sentry-readonly-allowlist` |
| any other server | any tool | not affected by this policy | |

## What this does not protect against

- **Reading sensitive data.** `view` covers ConfigMaps and pod logs, the role
  reads every table in the schema, and Sentry events can carry user data.
  Narrow the grants to what an investigation needs (RoleBindings per
  namespace, `GRANT SELECT` per table), and consider
  [sensitive data rules](../../sensitive-data.md) on tool results.
- **Functions.** A `SELECT` can call a function, and a `SECURITY DEFINER`
  function runs with its owner's rights. `verify-readonly-postgres.sh` lists
  the ones the role can run.
- **Session settings.** A session can change `default_transaction_read_only`
  and `statement_timeout` with `SET`, or start a `READ WRITE` transaction.
  They limit mistakes; the grants are what refuses writes to tables,
  sequences and schemas.
- **PUBLIC defaults in PostgreSQL.** Every role can create temporary tables,
  which last until the session ends, and large objects (`lo_create`,
  `lo_creat`, `lo_from_bytea`), which persist and which the role then owns
  and can change. No grant to the role controls either. PostgreSQL refuses
  both only while the transaction is read-only, which
  `default_transaction_read_only` makes the default and DBHub's `readonly`
  keeps (it refuses `BEGIN` and `SET`). To close them, as a
  superuser, run `REVOKE TEMPORARY ON DATABASE app_db FROM PUBLIC` and
  `REVOKE EXECUTE ON FUNCTION lo_create(oid), lo_creat(integer), lo_from_bytea(oid, bytea) FROM PUBLIC`,
  then grant them back to the roles that need them.
  `verify-readonly-postgres.sh` warns while they are open.
- **The SQL keyword guard.** It matches words, it does not parse SQL. It
  refuses some harmless queries, such as a string literal that contains the
  word "delete", and a function call can write without any of its keywords.
- **RBAC only.** `verify-readonly-rbac.sh` reads the rules that RBAC grants.
  If the cluster also uses a webhook authorizer, `kubectl auth can-i --list`
  says the list may be incomplete, and the script passes that warning on.
- **Other clients.** The row cap and the policy apply to calls through DBHub
  and Preloop. Anyone who can reach an MCP server's endpoint acts with its
  credential, so keep the endpoints reachable from Preloop only and turn on
  the servers' own authentication where they offer it.
- **Expensive reads.** A read can still load the database or the API server.
  `statement_timeout`, `lock_timeout` and the role's `CONNECTION LIMIT` bound
  it; they do not prevent it. They do not bound a session advisory lock
  (`pg_advisory_lock`), which every role can take and which outlives the
  statement; only the SQL guard refuses the advisory-lock functions.
- **Upstream changes.** A renamed read tool is denied until you add the new
  name. After upgrading a server, scan its tools and compare them with the
  lists in the policy, the server settings and the flow.

## Related

- [Governing back-office AI use](../../policies/back-office-ai.md)
- [Policy-as-code](../../concepts/policy-as-code.md)
- [Conditional approval (CEL)](../../approvals/cel-expressions.md)
- [Kubernetes user-facing roles](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#user-facing-roles)

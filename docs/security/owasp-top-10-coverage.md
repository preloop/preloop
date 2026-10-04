# OWASP Top 10 coverage: what Preloop does and does not do

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This page maps Preloop to the **OWASP Top 10 for Agentic Applications (2026)** and the
**OWASP Top 10 for LLM Applications (2026)**, entry by entry. It exists so that anyone
grading Preloop's claims (a buyer, an auditor, or a directory such as
[Yuntona](https://yuntona.ai)) can quote one stable paragraph per risk and check it
against the code.

Rules this page follows:

- Every risk is cited with its **taxonomy version**. `ASI02:2026` is not `ASI02`.
- Each entry states the **mechanism**, the **default posture** (what happens with no
  configuration), the **edition** the control ships in, and the **code path** in
  [github.com/preloop/preloop](https://github.com/preloop/preloop).
- Where Preloop does not address the observable property the OWASP entry describes,
  the entry says **Not covered**. A control that limits the consequences of a risk is
  labelled as such and is not presented as detection or prevention.
- Where Preloop's own behaviour is the exposure the entry describes, the entry says so
  under **Polarity**.
- Preloop is not a certification, a conformity assessment, or legal advice.

Edition labels used below: **OSS** is the Apache-2.0 repository. **Cloud** and **Enterprise** add users,
teams, RBAC, multi-approver workflows and AI-driven approvals. Unless stated
otherwise, a control below ships in OSS.

<!-- MAINTAINERS: do not reword the "Mechanism" paragraphs casually. External graders
hash the quoted span; every edit demotes their mapping until re-reviewed. Change them
when the behaviour changes. -->

## OWASP Top 10 for Agentic Applications, 2026

### ASI01:2026 Agent Goal Hijack

**Not covered at the level the entry describes.** Preloop does not maintain goal-state
or tool-use-pattern baselines and does not detect that an agent's objective has been
redirected.

**Consequence-limiting controls.** Every MCP tool call passes the [safety
layer](../guide/concepts/safety-layer.md): ordered access rules with CEL conditions on tool
arguments decide `allow`, `deny`, or `require_approval` before execution. A hijacked
agent can only do what its policy permits. Model I/O rules on `model.request` can
enable a prompt-injection detector toggle. Dedicated semantic prompt-injection
detection is on the roadmap and is not shipped.

Default posture: with no access rules, tool calls are allowed. Code:
`backend/preloop/services/policy_evaluator.py`, `backend/preloop/services/model_content_policy.py`.

### ASI02:2026 Tool Misuse and Exploitation

**Mechanism.** Preloop sits between the agent and every MCP tool. Access rules are
ordered, carry a priority, and evaluate CEL expressions over the tool's arguments,
not only its name (for example `args.amount > 2000` → deny, `args.amount > 100` →
require approval). The first matching rule wins. Denied calls return an MCP error the
agent can act on. Tools can additionally require a `justification` argument; when set
to `required`, calls without one are rejected. Applies equally to built-in, MCP-proxied
and HTTP tools.

Default posture: no rules → allow. Edition: OSS. Code:
`backend/preloop/services/policy_evaluator.py`, `backend/preloop/services/approval_helper.py`.
Docs: [Safety Layer](../guide/concepts/safety-layer.md), [Conditional Approval (CEL)](../guide/approvals/cel-expressions.md),
[Per-Tool Justification](../guide/approvals/justification.md).

### ASI03:2026 Identity and Privilege Abuse

**Mechanism.** Provider API keys and MCP server credentials are held in Preloop's
secret store, encrypted with an application key derived from `SECRET_KEY`; agents
never receive them. Onboarding mints a per-runtime credential, and the gateway issues
short-lived gateway tokens to enrolled runtimes. Tool lists, allowed-model lists and
budgets resolve per **subject** (API key or managed agent), so one runtime token sees
only the tools and models intended for it. A subscription credential stored on one
model cannot be used by another agent. Standard MCP clients authenticate with OAuth
2.1 and PKCE.

Default posture: account-level catalogue applies until a subject is scoped. Edition:
OSS; RBAC for humans is Cloud/Enterprise. Code:
`backend/preloop/services/subject_governance.py`, `backend/preloop/services/model_gateway_auth.py`,
`backend/preloop/services/secret_service.py`. Docs: [Subject-Scoped Governance](../guide/concepts/subject-scoped-governance.md),
[AI Model Gateway](../guide/concepts/model-gateway.md).

### ASI04:2026 Agentic Supply Chain Vulnerabilities

**Partially covered, different scope than most readers expect.** Preloop's SBOM Verify,
SBOM Exploit Check and Release Security Audit presets check the SBOM of the product
*your* build produced against OSV.dev and CISA KEV. They do not audit the agent's own
tool, plugin or MCP server chain.

What does apply to the agent's chain: an agent routed through Preloop can only reach
MCP servers registered in the account's tool catalogue, so the catalogue functions as
an allowlist of reachable tools. Preloop's own release assets carry Sigstore build
provenance and checksums.

Default posture: catalogue allowlist applies to any onboarded agent. Edition: OSS.
Docs: [External MCP Tools](../guide/tools/external-mcp.md), security audit presets in the
repository, `docs/release-verification.md`.

### ASI05:2026 Unexpected Code Execution

**Mechanism.** Shell and file tools are governed like any other tool: CEL conditions on
`args.command` (for example `args.command.contains('deploy') && args.command.contains('production')`)
can deny or hold a call for approval before it runs. For Claude Code, Preloop's plugin
puts native `Bash` and `Edit` calls behind the same approvals. Flow executions run in a
container Preloop launches, or on a private runner you operate.

Default posture: no rules → allow. Edition: OSS. Code:
`backend/preloop/services/policy_evaluator.py`. Docs: [Policy-as-Code](../guide/concepts/policy-as-code.md),
[Claude Code](../guide/clients/claude-code.md).

### ASI06:2026 Memory and Context Poisoning

**Not covered.** Preloop does not attribute memory writes to their source, track write
frequency, or inspect agent memory stores. `model.request` rules can inspect the prompt
sent to the provider, which is not the same property.

### ASI07:2026 Insecure Inter-Agent Communication

**Partially covered.** A flow can start a child flow of the same account. Children run
as the same account principal, resolve the same approval workflows, and any approval
they raise reaches the same humans, bounded by the account's approval deadline cap.
Operator notes delivered to a running agent are recorded with the identity of the
sender. Preloop does not authenticate, sign or inspect agent-to-agent messages that
do not pass through its gateway or MCP endpoint. Parent/child identity for harness
subagents is documented as observed behaviour only; nothing is enforced.

Edition: OSS. Docs: flow delegation and operator notes pages in the repository.

### ASI08:2026 Cascading Failures

**Mechanism (limit and recover).** The account kill switch halts an account by scope:
`gateway` rejects new model requests with HTTP 403 `preloop_account_halted`; `tools`
denies MCP dispatch including already-approved calls awaiting replay, and freezes
pending approval deadlines for exactly the time spent halted; `flows` blocks new
launches and durably requests termination of admitted executions. Hard budgets at
account, flow and subject level deny model calls before they reach the provider.
Child flow executions carry lineage columns, and the execution page renders the
delegation tree, so fan-out from one parent is visible.

Default posture: budgets and kill switch are off until configured/activated. Edition:
OSS. Code: `backend/preloop/services/kill_switch.py`,
`backend/preloop/services/model_gateway_budget_enforcer.py`,
`backend/preloop/services/flow_delegation_budget.py`. Docs: account kill switch page
in the repository, [Cost Analytics & Budgets](../guide/concepts/cost-analytics.md).

### ASI09:2026 Human-Agent Trust Exploitation

**Mechanism.** When a call is held for approval, the approver sees the tool name, the
arguments with sensitive fields redacted, the agent's `justification` if configured,
and the rule that gated the call. Approvals are delivered on mobile, watch, Slack,
Mattermost, email or webhook, and are recorded with approver identity and decision.

**Polarity.** AI-driven approval workflows (Enterprise) let a model decide approvals
against operator criteria. Decisions below the workflow's confidence threshold follow
its fallback (`escalate` to a human, `deny`, or `approve`). With a permissive threshold
or an `approve` fallback, an AI approver is itself a trust surface an agent could
exploit.

Edition: human approvals OSS; multi-approver, quorum and AI-driven approvals
Cloud/Enterprise. Code: `backend/preloop/services/approval_service.py`,
`backend/preloop/services/ai_approval_service.py`. Docs: [Multi-Channel Notifications](../guide/approvals/notifications.md),
[AI-Driven Approvals](../guide/approvals/ai-approvals.md).

### ASI10:2026 Rogue Agents

**Mechanism (contain and audit).** See ASI08 for the kill switch and budgets. Every tool
call and model call is attributed to a subject and recorded in one session timeline
with the matched rule, decision, approver and spend. Allowed-model lists per subject
prevent an agent from switching to a model it was not granted. Session search is
audited.

Default posture: attribution and timeline are always on for governed traffic; kill
switch and budgets require configuration. Edition: OSS. Code:
`backend/preloop/services/kill_switch.py`, `backend/preloop/services/session_manager.py`,
`backend/preloop/services/session_search_audit.py`. Docs: [Runtime Sessions](../guide/concepts/runtime-sessions.md).

## OWASP Top 10 for LLM Applications, 2026

<!-- MAINTAINERS: the LLM##:2026 identifiers below were confirmed on 2026-10-04 against
"OWASP Top 10 for LLM Applications 2026, v1.0" (the official PDF). The 2025 list used
different numbers for eight of ten entries, so never cite a bare `LLM##`: always write
`LLM##:2026`. -->

### LLM01:2026 Prompt Injection

Same position as ASI01:2026. Preloop limits what an injected instruction can do
through tool policy, approvals and redaction; it does not detect injection
semantically. Content-safety firewalls such as Lakera or Llama Guard can run in front
of the gateway.

<a id="sensitive-information-disclosure" name="sensitive-information-disclosure"></a>

### LLM02:2026 Sensitive Information Disclosure

**Polarity: Preloop is part of the disclosure surface described by this entry, and
this section describes what it does about it.**

The 2026 entry names observability tooling as a disclosure surface: "Observability platforms
(Langfuse, LangSmith, Datadog LLM Observability) log full prompts, completions, chunks, and
traces by default." (OWASP Top 10 for LLM Applications 2026, v1.0, LLM02:2026, page 20.)
Preloop's gateway event storage is that kind of store, which is why this entry is graded
against Preloop's own behaviour.

**Mechanism.** The model gateway records a normalized event per model call. When
`MODEL_GATEWAY_CAPTURE_CONTENT` is `true` (the default), the event includes a
**truncated content preview** (bounded by `MODEL_GATEWAY_MAX_PREVIEW_CHARS`) after
regex redaction of private keys, bearer tokens, `api_key`/`token`/`secret`/`password`
assignments, and common provider key formats. Set `MODEL_GATEWAY_CAPTURE_CONTENT=false`
to store no content. Approval notifications and tool execution records pass through
field-name redaction (`password`, `token`, `api_key`, `authorization`, `credential`,
`private_key`, `client_secret`, `webhook_secret` and similar). Free-text personal data
inside prompts is **not** redacted by default.

Default posture: content captured and redacted. With the default limits (32768
characters per message preview, 8192 characters per stored body string) most
conversations are stored in full after redaction. Edition: OSS. Code:
`backend/preloop/services/model_gateway_events.py`, `backend/preloop/utils/redaction.py`,
`backend/preloop/config.py` (`model_gateway_capture_content`). Docs:
[Security & Privacy](security-privacy.md), [Redaction](redaction.md), [AI Model Gateway](../guide/concepts/model-gateway.md).

### LLM03:2026 Excessive Agency

**Mechanism.** The MCP firewall (ASI02:2026), subject-scoped tool and model lists
(ASI03:2026) and human approvals (ASI09:2026) together bound what an agent may do.
Default posture: no rules → allow.

### LLM04:2026 Supply Chain

See ASI04:2026. Product-SBOM presets; catalogue allowlist for reachable MCP servers;
signed provenance on Preloop's own releases.

### LLM05:2026 Data and Model Poisoning

**Not covered.** Preloop has no control aimed at this entry.

### LLM06:2026 Unbounded Consumption

**Mechanism.** Gateway budgets at account, flow and subject level have soft and hard
limits; hard limits deny the call before it reaches the provider. Allowed-model lists
per subject stop an agent from moving to a more expensive model. Usage the gateway
cannot price is labelled unpriced, not $0.00. Default posture: no budget until
configured. Edition: OSS; per-user and per-team budgets Cloud/Enterprise. Code:
`backend/preloop/services/model_gateway_budget_enforcer.py`. Docs:
[Cost Analytics & Budgets](../guide/concepts/cost-analytics.md).

### LLM07:2026 Misinformation

**Not covered.** Preloop has no control aimed at this entry.

### LLM08:2026 Hidden Context Exposure

**Not covered.** The 2025 entry "System Prompt Leakage" is folded into this entry in
2026, and Preloop has no control aimed at it.

### LLM09:2026 Vector and Embedding Weaknesses

**Not covered.** Preloop has no control aimed at this entry.

### LLM10:2026 Improper Output Handling

**Mechanism.** Model I/O rules evaluate `model.response` after the provider returns
and before bytes reach the client, with the same `allow`/`deny`/`require_approval`
actions as tools. Default posture: no rules → allow. Edition: OSS. Code:
`backend/preloop/services/model_content_policy.py`.

## Changes to this page

Entries change when behaviour changes, with the release that changed it. Corrections:
open an issue on [github.com/preloop/preloop](https://github.com/preloop/preloop/issues).

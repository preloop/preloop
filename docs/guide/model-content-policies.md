# Model content policies

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Model I/O rules extend the existing policy engine so instance policies can
inspect model prompts and completions. Actions are the same as tools
(`allow`, `deny`, and `require_approval`) plus `notify`, which is only
available on model I/O rules (see [Notify-only rules](#notify-only-rules)).

This is policy on Preloop-governed traffic. It is not a standalone
guardrail product.

## Targets

Use these stable names:

- `model.request`: evaluated before the provider is called
- `model.response`: evaluated after the provider returns, before bytes
  reach the client

When no model I/O rules exist, traffic is allowed. That matches tool
evaluation when no access rule matches (`No access rules defined` /
`No rules matched (default allow)` in
`backend/preloop/services/policy_evaluator.py`).

## Console

`/console/policies` is the instance-wide authoring page (sidebar item
next to Tools), available by default to users with `view_policies` permission.
Operators can hide the page with `PRELOOP_POLICIES_CONSOLE=false`; instance
admins retain access and backend permission checks remain in force.
`/console/governance` redirects there. The per-tool
widget on Tools is unchanged.

Create, edit, disable, and delete rules from the Rules list. The
primary action is Describe a change: the account default model proposes
an edited YAML document. The UI shows a unified YAML diff against the
current export. Save applies it; Discard does not. Generation never
auto-applies.

The guided Add rule form includes:

- target (`model.request` or `model.response`)
- action (`allow`, `deny`, `require_approval`, `notify`; tool rules do not
  offer `notify`)
- the existing approval-workflow picker (not needed for `notify`)
- a condition field
- detector toggles for PII, injection, and moderation

YAML import and export on the same page round-trip these rules.

## Condition attributes

Canonical text fields:

- `request.text`: concatenated message contents (and Responses `input`
  when present)
- `response.text`: assembled assistant text plus returned reasoning/thinking text
  (including DeepSeek reasoning content, Anthropic thinking, and Responses
  reasoning summaries). Opaque encrypted reasoning and signatures are not text.

Also available:

- `model.id`, `model.provider`, `model.name`
- `session.id` when a runtime session is present
- `pii.found` (bool), `pii.types_found` (list), `pii.count` (int)
- `injection.score` (0-1), `injection.matched_patterns` (list)
- `moderation.flagged` (bool), `moderation.categories` (list)

Simple and CEL expressions work the same way as tool conditions.
Example: `pii.found == true`, `injection.score > 0.7`.

Injection scoring is best-effort. It reuses the deterministic
`security_screen` prompt-injection heuristics. It is not a guarantee.

## Detectors

Detectors run only when a rule enables them, or when a condition
references their attributes (`pii.`, `injection.`, `moderation.`).

| Detector | Default | Result attributes |
| --- | --- | --- |
| PII | email, phone, credit-card (Luhn) | `pii.found`, `pii.types_found`, `pii.count` |
| Injection | `security_screen` regex | `injection.score`, `injection.matched_patterns` |
| Moderation | local keyword ruleset (`local`) | `moderation.flagged`, `moderation.categories` |

### Sensitive data types

`pii.types` accepts every built-in type and the name of a custom entry
declared under `sensitive_data.detectors`. A rule that lists no types scans
the account default (`sensitive_data.detectors.types`), and without one the
original three.

| Type | What it matches |
| --- | --- |
| `email` | mailbox addresses |
| `phone` | E.164 plus US and European national formats |
| `credit_card` | 13 to 19 digits that pass the Luhn check |
| `iban` | IBANs with a valid mod-97 check |
| `ip_address` | IPv4 and IPv6 addresses that parse |
| `date_of_birth` | the nearest date to a birth keyword (DOB, born, Geburtsdatum, date de naissance, ...) |
| `national_id` | US SSN shape, DE tax id (ISO 7064 check), UK NINO, FR NIR (key), NL BSN (11-proef, keyword anchored); narrow with `locales: [us, de, uk, fr, nl]` |
| `medical_record_number` | an identifier after MRN or patient-id keywords; `medical_record_number_pattern` replaces the identifier shape |
| `person_name` | honorific or label anchored names (`Dr. X`, `patient: X`); low recall by design |

Account-defined entries:

```yaml
sensitive_data:
  detectors:
    types: [email, iban, employee_id]      # default scan set (optional)
    locales: [de, nl]                      # national_id locales (optional)
    custom_patterns:
      - name: employee_id
        regex: 'EMP-\d{6}'
        flags: [i]
    keywords:
      - name: codenames
        terms: ["Project Phoenix"]
        case_sensitive: false
```

Custom regexes are capped at 512 characters, must not nest quantifiers
(`(a+)+`) or use backreferences, and run with a hard per-text match
timeout; a pattern that exceeds it counts as a detector timeout for the
rule. Keyword lists match whole words only.

`GET /api/v1/policies/sensitive-data/types` lists every selectable type
with a label, description, example and locales. `POST
/api/v1/policies/sensitive-data/test` runs the detectors on sample text
and returns the match spans; the text is never logged or stored.

### Sensitive data rules on tool calls

`sensitive_data.rules` run the same detectors on MCP tool calls and, for
model targets, compile into ordinary model I/O rules so each path keeps one
evaluator.

```yaml
sensitive_data:
  rules:
    - id: block-cards-in-tools
      on: [tool.args, tool.result, model.request, model.response]
      scope: {agents: [], tools: [], servers: []}   # empty means all
      types: [credit_card, iban]                    # default: the detectors block types
      action: deny                                  # notify | deny | require_approval
    - id: ask-before-sharing-records
      on: [tool.result]
      scope: {tools: [get_patient_record], servers: [ehr]}
      action: require_approval
      approval_workflow: humans
```

- `scope.agents` holds managed agent ids, `scope.tools` client-visible tool
  names, `scope.servers` MCP server names (case-insensitive; builtin tools
  belong to `preloop-mcp`). Model targets honour `agents` only.
- `tool.args` runs before the call. `deny` refuses with the types found
  (never the values); `require_approval` creates an approval through the
  rule's workflow or the account default and fails closed without one;
  `notify` records a notice and lets the call through. Tool access-rule
  conditions can read `pii.found`, `pii.types_found`, `pii.count` and
  `pii.paths` from the scan next to `args`.
- `tool.result` runs after the call. `deny` replaces the result with a
  refusal; `require_approval` holds the result until a human decides.
- Audit rows carry the rule id, target, types found, argument paths and a
  SHA-256 of the scanned text. With no rule in scope no detector runs.

Each rule has `detector_timeout_ms` (default 500) and
`on_detector_timeout` (default `deny`, fail closed). Set
`on_detector_timeout: allow` to skip that rule on timeout. A rule whose conditions are all `notify` never blocks on
timeout: the rule is skipped.

Application logs never include full prompts. Audit and approval tickets
store the rule id, detector summary, and a SHA-256 of the scanned text.
They do not store a raw preview (the preview can be the PII the rule
blocked).

## Streaming

Response rules use buffer-until-assembled: the gateway holds SSE events
until `response.text` is complete, evaluates policy, then either replays
the buffered events or returns a deny error. The blocked payload is
never sent to the client. When no `model.response` rules exist, streams
pass through without buffering.

Buffering is required for `deny` and `require_approval`. Tokens already
sent to the client cannot be retracted, so a rolling window cannot
enforce those actions (it can miss PII from earlier chunks and cannot
unsend them). The tradeoff: time-to-first-token becomes time-to-last-token
when any `model.response` rule is enabled. Issue #313 allowed either
buffer-until-assembled or a rolling window; this implementation keeps
buffering for correctness of deny.

When every enabled `model.response` rule is notify-only, the stream is
not buffered. Events reach the client as they arrive and the rules are
evaluated once the stream ends (or the client disconnects). If any
enabled response rule can deny or require approval, the stream is
buffered as above and notify conditions are evaluated on the same
assembled text.

## Deny errors

Denied requests return HTTP 403 with:

- `code`: `content_policy_denied`
- `message`: `Blocked by content policy (rule <id>)`

OpenAI-compatible clients can surface that message.

## Approvals

`require_approval` uses the existing tool-approval workflow
(`require_approval` / `approval_service`). The hold appears in the
existing approvals inbox. The ticket includes rule id, detector
summary, and a SHA-256 of the scanned text. It does not store the full
prompt or a raw preview. HTTP workers and background optimization jobs
run the hold on the application event loop. Callers with no loop fail
closed instead of creating a temporary loop. A required approval pauses
a background optimization worker for the approval window (default 5
minutes, configurable up to 30 days).

## Notify-only rules

A condition with `action: notify` lets the call through exactly like
`allow`: no approval, no buffering, no error to the client. It records
that the rule matched and tells the policy owners.

Evaluation order:

- A matching notify condition records one hit for its rule and
  evaluation continues with the next rule. A rule records at most one
  hit per evaluation, even when several of its notify conditions match.
- `deny`, `require_approval` and `allow` still use first match wins. A
  notify rule listed before the terminal match still records its hit
  (so a denied call can also produce a notice). Rules after the terminal
  match are not evaluated.
- A notify condition whose expression fails to evaluate is skipped.
- When only notify rules matched, the decision is `notify`, which
  proceeds like `allow`.

Each hit is stored in the `policy_notice_hit` table with the account,
user (when known), target, rule id and description, time, the SHA-256
of the scanned text, and an excerpt of at most 280 characters around the
match. The excerpt goes through the same secret redaction as the
gateway logs. If redaction fails, only the hash and rule id are kept.
The full text is never stored. When the EE audit plugin is installed,
each hit also writes an audit row with action `notify`.

Policy owners are told about a hit through:

- email, for owners whose notification preferences leave email on
- mobile push, for owners with `enable_mobile_push` on
- the rule's approval workflow (or the account default) when that
  workflow is of type Slack, Mattermost or webhook

Owners are active users who are the account's primary user, a
superuser, or hold the `manage_policies` permission. The message names the rule, includes the
redacted excerpt, and has no approve or deny link. Messages are
debounced to one per rule, per user, per hour. Hits are still recorded
every time.

Where hits show up:

- Attention: one "Policy notice" card per rule with hits in the last 7
  days. Dismissing it hides it until the rule matches again.
- Optimization digest: a "Policy notices" section with each rule's hit
  count over one window, the last user and the last excerpt. The window is
  the 7 days ending at `now` by default, or exactly the `start` and `end`
  the caller passes; both bounds are always applied, and the section
  reports the window it covered as `window_start` and `window_end`. The
  digest is sent by the EE optimization plugin; without it, nothing is
  sent.
- `GET /api/v1/policies/notices/summary?days=7` (requires
  `view_policies`).

Example:

```yaml
model_io:
  - id: notify-codename
    description: Mentions of the internal codename
    target: model.request
    conditions:
      - expression: "request.text.contains('project-x')"
        action: notify
```

`notify` is rejected on tool rules.

## YAML example

```yaml
version: "1.0"
metadata:
  name: Content policies
approval_workflows:
  - name: high-risk
    timeout_seconds: 300
model_io:
  - id: deny-pii-in-prompts
    target: model.request
    detectors:
      pii:
        types: [email, phone, credit_card]
    conditions:
      - expression: "pii.found == true"
        action: deny
  - id: approve-flagged-output
    target: model.response
    approval_workflow: high-risk
    detectors:
      moderation: true
    conditions:
      - expression: "moderation.flagged == true"
        action: require_approval
  - id: deny-injection
    target: model.request
    detectors:
      injection: true
    conditions:
      - expression: "injection.score > 0.7"
        action: deny
```

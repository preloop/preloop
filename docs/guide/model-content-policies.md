# Model content policies

Model I/O rules extend the existing policy engine so instance policies can
inspect model prompts and completions. Actions are the same as tools:
`allow`, `deny`, and `require_approval`.

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
next to Tools). `/console/governance` redirects there. The per-tool
widget on Tools is unchanged.

Create, edit, disable, and delete rules from the Rules list. The
primary action is Describe a change: the account default model proposes
an edited YAML document. The UI shows a unified YAML diff against the
current export. Save applies it; Discard does not. Generation never
auto-applies.

The guided Add rule form includes:

- target (`model.request` or `model.response`)
- action (`allow`, `deny`, `require_approval`)
- the existing approval-workflow picker
- a condition field
- detector toggles for PII, injection, and moderation

YAML import and export on the same page round-trip these rules.

## Condition attributes

Canonical text fields:

- `request.text`: concatenated message contents (and Responses `input`
  when present)
- `response.text`: assembled assistant text

Also available:

- `model.id`, `model.provider`, `model.name`
- `session.id` when a runtime session is present
- `pii.found` (bool), `pii.types_found` (list)
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
| PII | email, phone, credit-card (Luhn) | `pii.found`, `pii.types_found` |
| Injection | `security_screen` regex | `injection.score`, `injection.matched_patterns` |
| Moderation | local keyword ruleset (`local`) | `moderation.flagged`, `moderation.categories` |

Each rule has `detector_timeout_ms` (default 500) and
`on_detector_timeout` (default `deny`, fail closed). Set
`on_detector_timeout: allow` to skip that rule on timeout.

Application logs never include full prompts. Audit and approval tickets
store the rule id, detector summary, a SHA-256 of the text, and an
80-character preview.

## Streaming

Response rules use buffer-until-assembled: the gateway holds SSE events
until `response.text` is complete, evaluates policy, then either replays
the buffered events or returns a deny error. The blocked payload is
never sent to the client. When no `model.response` rules exist, streams
pass through without buffering.

## Deny errors

Denied requests return HTTP 403 with:

- `code`: `content_policy_denied`
- `message`: `Blocked by content policy (rule <id>)`

OpenAI-compatible clients can surface that message.

## Approvals

`require_approval` uses the existing tool-approval workflow
(`require_approval` / `approval_service`). The hold appears in the
existing approvals inbox. The ticket includes rule id and detector
summary. It does not store the full prompt.

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

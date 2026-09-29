# OpenClaw Policy Examples

Example policies for using Preloop as a safety layer with [OpenClaw](https://github.com/openclaw/openclaw).

For setup instructions, see the **[OpenClaw Integration Guide](../../../integrations/openclaw.md)**.

## Policies

| Policy | File | Purpose |
|--------|------|---------|
| Shell Command Safety | [shell-approval.yaml](shell-approval.yaml) | Require approval for destructive shell commands |
| Sensitive File Protection | [file-protection.yaml](file-protection.yaml) | Protect writes to sensitive paths |
| Browser Safety | [browser-safety.yaml](browser-safety.yaml) | Approval for browser actions on sensitive sites |
| Integration Guide | [integration-guide.yaml](integration-guide.yaml) | Comprehensive example with all tool types |

## Apply

Download a policy file, then validate and apply it with the CLI:

```bash
preloop policy validate shell-approval.yaml
preloop policy apply shell-approval.yaml
preloop policy apply -r .                  # apply every policy in a directory
preloop policy diff shell-approval.yaml    # preview against the live account
```

## Customize

Each policy uses [CEL expressions](../../../approvals/cel-expressions.md). Edit
`condition.expression` fields to match your security requirements and replace
placeholder approvers with your users and teams.

## Related

- [OpenClaw Integration Guide](../../../integrations/openclaw.md)
- [CEL Expression Reference](../../../approvals/cel-expressions.md)
- [Team-Based Approvals](../../../approvals/teams.md)

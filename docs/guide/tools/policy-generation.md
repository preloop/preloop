# Policy Generation

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Generate Preloop policy YAML using AI from natural-language descriptions or historical audit-log patterns.

---

## Overview

Instead of writing policy YAML by hand, you can describe what you want in plain English and let an AI model generate a valid policy for you. You can also generate policies based on your actual tool usage patterns from audit logs.

!!! info "Requirements"
    At least one AI model must be configured in your Preloop account (**Models > Add model**).

---

## CLI Usage

### Generate from a Prompt

```bash
preloop policy generate "require approval for any payment over $500"
```

Or read the prompt from a file:

```bash
preloop policy generate --file prompt.txt
```

### Generate from Audit Logs

Analyse your historical tool-call patterns:

```bash
preloop policy generate --from-audit-logs
```

With a date range:

```bash
preloop policy generate --from-audit-logs \
  --start-date 2026-01-01 --end-date 2026-02-01
```

### Write to File

```bash
preloop policy generate "deny all destructive tools" -o policy.yaml
```

Then apply it:

```bash
preloop policy apply policy.yaml
```

### Flags

| Flag | Description |
|------|-------------|
| `-o, --output` | Write output to a file instead of stdout |
| `-f, --file` | Read prompt from a file |
| `--from-audit-logs` | Generate from audit-log patterns |
| `--start-date` | Filter audit logs after this date (ISO format) |
| `--end-date` | Filter audit logs before this date |
| `--no-context` | Don't include current account config as LLM context |

### Starter Policy for an MCP Server

Generate a policy scoped to a single MCP server and its discovered tools, preferring approvals for mutating or high-impact tools:

```bash
preloop agents starter-policy github
```

Write to a file, or apply directly:

```bash
preloop agents starter-policy github -o github-policy.yaml
preloop agents starter-policy github --apply           # preview, confirm, apply
preloop agents starter-policy github --apply --yes     # skip the confirmation
preloop agents starter-policy github --apply --dry-run # validate only
```

| Flag | Description |
|------|-------------|
| `-o, --output` | Write generated policy YAML to a file |
| `--apply` | Apply the generated policy immediately |
| `--dry-run` | With `--apply`, validate without applying changes |
| `--yes` | Skip the apply confirmation prompt |
| `--no-context` | Don't include current account config as LLM context |

---

## Web UI Usage

1. Open **Policies** and click **Describe a change**
2. Choose the **From Description** or **From Audit Logs** tab
3. Enter your prompt, or pick an optional start and end date
4. Click **Generate**
5. Review the generated YAML and its diff against the live policy (**Download** saves it as a file)
6. Click **Save**, review the changes, and click **Apply changes** to activate it

---

## API Endpoints

### Generate from Prompt

```
POST /api/v1/policies/generate
```

```json
{
  "prompt": "require approval for any payment over $500",
  "include_current_config": true
}
```

### Generate from Audit Logs

```
POST /api/v1/policies/generate-from-audit
```

```json
{
  "start_date": "2026-01-01",
  "end_date": "2026-02-01"
}
```

### Response

```json
{
  "yaml": "version: \"1.0\"\nmetadata:\n  name: ...",
  "warnings": ["Some optional warnings"]
}
```

---

## How It Works

1. **Context gathering**: The system collects your account's MCP servers, registered tools, and current policy (optional)
2. **LLM generation**: Your default AI model generates valid policy YAML matching the Preloop schema
3. **Validation**: The output is validated against the PolicyDocument schema
4. **Preview**: You review and optionally edit the YAML before applying

For audit-log generation:
1. **Pattern analysis**: Historical tool calls are summarised (frequency, users, outcomes)
2. **LLM generation**: The summary is sent to the LLM with instructions to create appropriate rules
3. **Same validation and preview flow**

---

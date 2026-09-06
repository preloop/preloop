# Flow Presets

This directory contains YAML files defining flow presets that are created for new accounts.

## File Naming Convention

Preset files should be named with a numeric prefix to control the order they appear:

```
01-issue-triage.yml
02-pr-reviewer.yml
03-implementation.yml
```

## File Format

Each YAML file should define a single flow preset:

```yaml
slug: "issue-triage-assistant"   # Stable identity used for layering/override
name: "Issue Triage Assistant"
description: "Automatically analyze new issues..."
icon: "funnel"
trigger_event_source: null  # Set to tracker_id when instantiated
trigger_event_types:         # Array of event types that trigger this flow
  - "issue_opened"
  - "issue_updated"
prompt_template: |
  You are an intelligent issue triage assistant.
  ...
agent_type: "codex"
agent_config:
  sandbox_type: "exec"
  enable_auto_lint: false
allowed_mcp_servers: []
allowed_mcp_tools:
  - name: "search_issues"
  - name: "get_issue"
# Codex/Gemini/OpenCode always attach Preloop MCP. Tool enablement is
# the allowlist. Name-only entries are the legacy shape and mean
# Preloop builtins on preloop-mcp.
git_clone_config: null
timeout_seconds: 1800          # Optional per-flow wall-clock budget
is_preset: true
```

### `timeout_seconds`

Optional. The wall-clock budget for one execution of the flow, in seconds,
between 60 and 86400. Omit it (or set `null`) to use the deployment default
from `FLOW_EXECUTION_MAX_WAIT_SECONDS`, which is 3600.

Set it when the preset's work has a shape the default does not fit: a review
that should finish in minutes benefits from a short budget (a run that
overruns it is stuck, and failing early frees the slot and tells the author),
while a long audit needs a longer one (the default was cutting genuine runs
off mid-scan). When a run exceeds its budget the failure message names the
budget that expired, so an operator can tell "stuck" from "needs more time".

> **Note:** Use `trigger_event_types` (plural, array) not the legacy
> `trigger_event_type` (singular). The singular form is ignored by the
> schema and flows created with it will never match events.

## Layered Preset Directories

`PRELOOP_PRESETS_PATH` accepts an `os.pathsep`-separated list of directories
(e.g. `/app/backend/presets:/app/presets` on Linux), loaded in order and
merged with union semantics:

- **Identity**: every preset has a stable `slug`. Declare it explicitly in
  the YAML (recommended); otherwise it is derived from the filename stem
  minus the numeric prefix (`004-docs-generator.yaml` -> `docs-generator`).
- **Union**: presets with distinct slugs from all directories appear in the
  catalog.
- **Override**: on slug collision, the preset from the *later* directory
  fully replaces the earlier one. Its numeric filename prefix determines the
  catalog position.
- **Tombstone**: a preset with `disabled: true` in a later directory
  suppresses the same-slug preset from earlier directories and is itself not
  shown.
- **Ordering**: the catalog is stably sorted by (numeric prefix, slug).

The `slug` is loader-internal identity only: it is stripped from the loaded
preset data before the catalog is handed to the rest of the application.

A single-directory value (the default) behaves like the historical
single-directory loader, except that presets are now de-duplicated by slug
even within one directory: two files in the same directory that resolve to
the same slug (e.g. `001-triage.yaml` and `002-triage.yaml`) collide — the
later file wins and a warning is logged at startup.

## Notes

- The `is_preset` field defaults to `true` if not specified
- Presets are loaded at application startup and cached
- Invalid YAML files will cause a startup error

## Issue readiness and completion auditing

`012-issue-refinement-readiness.yaml` produces a structured, reviewable acceptance
contract after triage. `013-merged-issue-completion-audit.yaml` independently audits
the exact revision that closed an issue through a verified merged PR. Both use the
public lifecycle controller; neither requires private presets or plugins. Configure
project policy, scoped repository checkout and approved test environments as
explained in [the lifecycle guide](../../docs/guide/flows/issue-lifecycle.md).

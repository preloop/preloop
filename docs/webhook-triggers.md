# Webhook Triggers

Flows can be triggered by an inbound webhook:

```
POST /webhooks/flows/{flow_id}/{webhook_secret}
Content-Type: application/json
```

The JSON body becomes the trigger payload. Prompt templates can reference it
with `{{trigger_event.payload.<path>}}` (or `{{trigger_event}}` for the whole
event), and the payload is snapshotted onto the execution record
(`trigger_event_details`) for audit.

## Seeding `/workspace` files (`workspace_files`)

Instead of embedding large fixtures into the prompt via
`{{trigger_event.payload.*}}` (brittle, token-expensive), the payload may
declare files to materialize in the agent's `/workspace` volume before the
agent starts:

```json
{
  "workspace_files": [
    {"path": "fixtures/input.json", "content_base64": "eyJrZXkiOiAiLi4uIn0="}
  ],
  "any_other_payload_fields": "still available to prompt templates"
}
```

Each entry:

| Field | Description |
| --- | --- |
| `path` | Destination relative to `/workspace`. Forward slashes only. |
| `content_base64` | File content, standard base64 (whitespace-wrapped input is tolerated). v1 is inline-only — URLs are not supported. |

Files are written **after** the flow's git clone step and **before** any
custom setup commands, so cloned repos do not sweep the seeds away and setup
commands can consume them.

### Validation rules

An invalid declaration fails the execution with a clear error message before
any agent container starts:

- **Relative paths only** — absolute paths, `~`, backslashes, control
  characters, and any `..` traversal are rejected.
- **No `.git` segment at any depth** (e.g. `.git/config`,
  `client/.git/hooks/post-commit`) — seeds cannot touch git metadata of
  cloned repositories.
- **No duplicate paths** (after normalization).
- **Strict base64** for `content_base64`.
- **Size cap: 1 MiB total base64-encoded** across all files (~768 KiB
  decoded). The encoded form is embedded in the container launch command —
  on Kubernetes that lives in the Job spec, which must stay well under
  etcd's ~1.5 MiB object limit — so the cap applies to the encoded size.
- **File-count cap: 50 files** per payload.

At runtime the materialization step re-checks physical containment: writes
that would resolve outside `/workspace` through a symlink in the cloned
workspace (including a symlinked target file) are refused and fail the
execution.

### Audit & prompt hygiene

- The validated path list is stamped onto the execution record under
  `trigger_event_details._workspace_file_paths`.
- Full-event prompt embeds (`{{trigger_event}}` /
  `{{trigger_event.payload}}`) redact each `content_base64` so fixture
  blobs never inflate prompts.

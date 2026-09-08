# Webhook Triggers

Flows can be triggered by an inbound webhook:

```
POST /webhooks/flows/{flow_id}/{webhook_secret}
Content-Type: application/json
```

The JSON body becomes the trigger payload. Prompt templates can reference it
with `{{trigger_event.payload.<path>}}` (or `{{trigger_event}}` for the whole
event), and the payload is snapshotted onto the execution record
(`trigger_event_details`) for audit. Reserved keys in the body
(`_matrix`, `_model_routing`, `ai_model_id`, `assessment`) are not treated as
authorized model or harness overrides, including when nested under the
webhook `payload`. Presence of `_resume` in the body is also not a trust
signal.

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

The declaration is validated at trigger time. `POST /flows/{flow_id}/trigger`
and the webhook trigger endpoint reject an invalid declaration with `400` and
an oversized one with `413`, naming the cap, the actual size and the overage.
No execution record is created. Trigger paths that do not go through those
endpoints fail the execution with the same message before any agent container
starts.

- **Relative paths only**: absolute paths, `~`, backslashes, control
  characters, and any `..` traversal are rejected.
- **No `.git` segment at any depth** (e.g. `.git/config`,
  `client/.git/hooks/post-commit`), so seeds cannot touch git metadata of
  cloned repositories.
- **No duplicate paths** (after normalization).
- **Strict base64** for `content_base64`.
- **Per-file cap: 96 KiB base64-encoded** (~72 KiB decoded). Each file
  travels as one container environment variable, and Linux caps a single
  `execve` string (`MAX_ARG_STRLEN`) at 128 KiB.
- **Total cap: 1 MiB base64-encoded** across all files (~768 KiB decoded).
  On Kubernetes the environment lives in the Job spec, which must stay well
  under etcd's ~1.5 MiB object limit.
- **File-count cap: 50 files** per payload.

Both size caps apply to the **encoded** form, because that is what the
transport carries. Neither budget is shared with the rendered prompt: seed
contents are passed in the environment, and the launch command references
them by variable name. A large prompt does not shrink the seed allowance.

If your artefacts do not fit, gzip them and have the flow decompress in a
setup command. Do not reshape the artefact itself to fit the transport: a
CRA evidence pack that audits an edited SBOM records findings about the
edit, not about the product.

At runtime the materialization step re-checks physical containment: writes
that would resolve outside `/workspace` through a symlink in the cloned
workspace (including a symlinked target file) are refused and fail the
execution. The workspace root itself is canonicalized first, so images
where `/workspace` resolves through a symlink work normally.

### Audit & prompt hygiene

- The validated path list is stamped onto the execution record under
  `trigger_event_details._workspace_file_paths`.
- Full-event prompt embeds (`{{trigger_event}}` /
  `{{trigger_event.payload}}`) redact each `content_base64` so fixture
  blobs never inflate prompts.

### See also

- [Security audit presets](guide/flows/security-audit-presets.md) — CI-fed
  SBOM verification, exploit checking, and release audits that consume
  `workspace_files`-seeded artifacts.

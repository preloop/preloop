# Session artifacts

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

An artifact is a file an agent produced or captured during a runtime session:
a screenshot, a call transcript, a summary document, a generated report, a
Playwright trace. Preloop stores it encrypted on the session, records an
`artifact` row on the session timeline, and serves the bytes back to anyone in
the account who may read runtime sessions.

An artifact is evidence of what the agent said it produced. Storing one is not
an approval and does not prove the content is correct.

## Kinds and caps

Every artifact has a kind. The kind fixes the media types it accepts and the
largest size it may have. For most media types the first bytes are compared
with the format signature, so a file that is not the declared type is refused.

| Kind | Media types | Default cap | Setting |
| --- | --- | --- | --- |
| `screenshot` | `image/png`, `image/jpeg`, `image/webp` | 2 MiB | `RUNTIME_SESSION_SCREENSHOT_MAX_BYTES` |
| `recording` | `video/webm`, `video/mp4` | 512 MiB | `RUNTIME_SESSION_RECORDING_MAX_BYTES` |
| `screencast` | `video/webm`, `video/mp4` | 64 MiB | `RUNTIME_SESSION_SCREENCAST_MAX_BYTES` |
| `audio` | `audio/mpeg`, `audio/wav`, `audio/ogg`, `audio/webm`, `audio/mp4`, `audio/flac` | 25 MiB | `RUNTIME_SESSION_AUDIO_MAX_BYTES` |
| `transcript` | `text/plain`, `text/vtt`, `application/x-subrip`, `application/json` | 5 MiB | `RUNTIME_SESSION_TRANSCRIPT_MAX_BYTES` |
| `document` | `text/plain`, `text/markdown`, `application/json`, `application/pdf` | 10 MiB | `RUNTIME_SESSION_DOCUMENT_MAX_BYTES` |
| `generated_file` | any type except native executables (ELF, PE, Mach-O) | 10 MiB | `RUNTIME_SESSION_GENERATED_FILE_MAX_BYTES` |
| `trace` | `application/zip` (for example a Playwright `trace.zip`) | 25 MiB | `RUNTIME_SESSION_TRACE_MAX_BYTES` |

When a deposit names no kind, Preloop infers one from the content: an image is
a `screenshot`, audio is `audio`, video is a `recording`, `text/vtt` and
`application/x-subrip` are a `transcript`, other text is a `document`, and
anything else is a `generated_file`.

A request body may be at most the largest kind cap plus 1 MiB. Larger bodies
are refused before they are parsed.

## Labels

Labels are a small JSON object you attach at deposit time and filter by later.
At most 16 keys; each key matches `^[a-z][a-z0-9_.-]{0,62}$`; each value is a
string of up to 128 characters, except `tags`, which is a list of up to 16
such strings. These keys are reserved and have a documented meaning:

| Key | Meaning |
| --- | --- |
| `site` | Physical or logical site the artifact belongs to, for example a plant. |
| `tenant_ref` | Your own customer or tenant reference. |
| `consent_basis` | Why recording this person is allowed, for example `contract`. |
| `retention_class` | Retention bucket name, enforced by the Enterprise records policy. |
| `tags` | Free-form list of short strings. |

Any other key that follows the rules is stored as given. A label that breaks a
rule refuses the whole deposit with `artifact_labels_invalid`.

## Depositing an artifact

All three paths store through the same service, write the same timeline row
and return the same error codes. The session always comes from the
credential: an agent can only deposit on its own session.

### Get a session-bound key

The examples below use a runtime session token. A console user (or a launcher
acting for one) mints it:

```bash
curl -X POST "$PRELOOP_URL/api/v1/auth/runtime-sessions/token" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "session_source_type": "claude_code",
    "session_source_id": "docs-demo-4",
    "expires_in_minutes": 60,
    "allowed_mcp_tools": ["deposit_artifact"]
  }'
```

```json
{"runtime_session_id":"a30cd80e-...","token":"flow_vJlp...","expires_at":"2026-10-01T17:07:22Z","session_source_type":"claude_code","session_source_id":"docs-demo-4","session_reference":null}
```

Use `runtime_session_id` as `$SESSION_ID` and `token` as `$AGENT_KEY`. A
managed agent key that is pinned to a session works the same way.

### 1. MCP tool `deposit_artifact`

`deposit_artifact` is a built-in MCP tool, **off by default**. Turn it on for
the account on the **Tools** page, or list it in a flow's `allowed_mcp_tools`.
A session token keeps only the tools the account had enabled when it was
minted, so mint (or re-mint) the token after enabling the tool and ask for it
in `allowed_mcp_tools` as above.

Claude Code:

```bash
claude mcp add --transport http preloop https://preloop.example.com/mcp/v1 \
  --header "Authorization: Bearer $AGENT_KEY"
```

opencode, in `opencode.json` (project) or `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "preloop": {
      "type": "remote",
      "url": "https://preloop.example.com/mcp/v1",
      "headers": { "Authorization": "Bearer {env:AGENT_KEY}" }
    }
  }
}
```

Then ask the agent to save its work, or call the tool directly. The arguments
are one MCP `ContentBlock` plus a name, an optional kind and labels:

```json
{
  "name": "deposit_artifact",
  "arguments": {
    "name": "notes.md",
    "kind": "document",
    "labels": {"site": "heilbronn"},
    "content": {"type": "text", "text": "# Notes\nRestarted line 3."}
  }
}
```

`content.type` is `text`, `image` (`data`, `mimeType`), `audio` (`data`,
`mimeType`), `resource` (`resource.uri`, `resource.mimeType`, and `text` or
base64 `blob`), or `resource_link` to an artifact of the same session, which
stores a copy with new labels as a child of the linked one. The result has one
`resource_link` content block pointing at the stored bytes and the full
artifact descriptor as `structuredContent`:

```json
{"content":[{"type":"resource_link","name":"notes.md","uri":"http://localhost:18090/api/v1/runtime-sessions/a30cd80e-.../artifacts/88b4b89d-...","mimeType":"text/plain","size":25,"_meta":{"preloop.dev/artifact":{"artifact_id":"88b4b89d-...","kind":"document","labels":{"site":"heilbronn"},"sha256":"129f22f5...","producer":"deposit_mcp"}}}],"structuredContent":{"id":"88b4b89d-...","kind":"document","availability":"available","...":"..."}}
```

Refusals are tool errors whose text starts with the error code.

### 2. REST

`POST /api/v1/runtime-sessions/{runtime_session_id}/artifacts` with the agent
key. The response is `201` with the artifact descriptor.

JSON, with the content as an MCP `ContentBlock`:

```bash
curl -X POST "$PRELOOP_URL/api/v1/runtime-sessions/$SESSION_ID/artifacts" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: call-42-vtt" \
  -d "{
    \"name\": \"call.vtt\",
    \"labels\": {\"consent_basis\": \"contract\"},
    \"content\": {
      \"type\": \"resource\",
      \"resource\": {
        \"uri\": \"file:///call.vtt\",
        \"mimeType\": \"text/vtt\",
        \"blob\": \"$(base64 < call.vtt | tr -d '\n')\"
      }
    }
  }"
```

```json
{"id":"5898d690-...","runtime_session_id":"5e25eed1-...","activity_id":"ea2154a4-...","kind":"transcript","name":"call.vtt","content_type":"text/vtt","size_bytes":67,"sha256":"dfd59948...","labels":{"consent_basis":"contract"},"producer":"deposit_api","agent_id":"7deec1ef-...","tool_name":null,"parent_artifact_id":null,"text_status":"none","availability":"available","legal_hold":false,"created_at":"2026-10-01T16:06:48.135281Z","content_block":{"type":"resource_link","uri":"/api/v1/runtime-sessions/5e25eed1-.../artifacts/5898d690-...","name":"call.vtt","mimeType":"text/vtt","size":67,"_meta":{"preloop.dev/artifact":{"artifact_id":"5898d690-...","kind":"transcript","labels":{"consent_basis":"contract"},"sha256":"dfd59948...","producer":"deposit_api"}}}}
```

The optional `Idempotency-Key` header makes a retry safe: the same key on the
same session returns the original artifact with `201` and the header
`Idempotent-Replayed: true`, and stores nothing new.

A plain text body is the shortest form:

```bash
curl -X POST "$PRELOOP_URL/api/v1/runtime-sessions/$SESSION_ID/artifacts" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "call-summary.md", "kind": "document",
       "labels": {"site": "heilbronn", "tags": ["shift-a"]},
       "content": {"type": "text", "text": "# Summary\n\nLine 3 stopped at 06:10."}}'
```

Multipart, for files on disk. The `file` part carries the bytes and its media
type; the optional `metadata` part is the same fields as JSON, without
`content`. `name` defaults to the file name:

```bash
curl -X POST "$PRELOOP_URL/api/v1/runtime-sessions/$SESSION_ID/artifacts" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -F "file=@call.vtt;type=text/vtt" \
  -F 'metadata={"kind":"transcript","labels":{"site":"heilbronn"}}'
```

Optional fields in both encodings: `activity_id` (a timeline row of this
session the artifact illustrates), `parent_artifact_id` (an artifact of this
account it derives from, for example a summary of a transcript) and
`tool_name`.

List a session's artifacts, newest first, with the agent key or a console
user token:

```bash
curl "$PRELOOP_URL/api/v1/runtime-sessions/$SESSION_ID/artifacts?kind=transcript&label=site:heilbronn&limit=50" \
  -H "Authorization: Bearer $USER_TOKEN"
```

```json
{"items":[{"id":"65cd256c-...","kind":"transcript","name":"call.vtt","labels":{"site":"heilbronn"},"availability":"available","...":"..."}],"next_cursor":null}
```

`label` is repeatable (`key:value`), `limit` is 1 to 200 (default 50), and
`next_cursor` goes into `cursor` for the next page.

Read the bytes with the `content_block.uri`:

```bash
curl "$PRELOOP_URL/api/v1/runtime-sessions/$SESSION_ID/artifacts/$ARTIFACT_ID" \
  -H "Authorization: Bearer $USER_TOKEN"
```

The response has the stored media type and `Cache-Control: private,
max-age=300`. It is 410 with `{"availability": "evicted"}` or
`{"availability": "expired"}` once the bytes are gone.

The full request and response schemas are in the API reference
(`openapi.yaml`, operation `deposit_runtime_session_artifact`).

### 3. CLI

`preloop artifacts put <file> --session <id> --kind <kind> --label site=...`
is planned in #1089. Until it ships, use the multipart call above.

## Where artifacts appear

- **Session timeline.** Every deposit writes an `artifact` row on the session
  timeline, in time order between the model turns and tool calls around it.
  The console renders these rows with a kind icon, the name, labels, a text
  excerpt or thumbnail, and a header count with a filter per kind (#1083).
  A session without artifacts says so in the header and links to this page.
- **Activity API.** `GET /api/v1/runtime-sessions/{id}/activity` returns the
  same `artifact` rows with an artifact summary in `metadata.artifact`: `id`,
  `kind`, `name`, `content_type`, `size_bytes`, `labels` and `producer`. The
  full descriptor (`sha256`, `availability`, `legal_hold`,
  `parent_artifact_id` and so on) comes from
  `GET /api/v1/runtime-sessions/{id}/artifacts`.
- **Storage card.** **Settings > Account** shows the session artifact storage
  used per kind against the account budget.
- **Artifacts page.** A cross-session page with search and filters is planned
  (memo phase 2).

Browser step screenshots are artifacts of kind `screenshot` too; see
[Browser steps](browser-agents.md) for how the console shows them.

## Retention, legal hold, budget and eviction

- **Budget.** Each account has one plaintext budget for all session artifacts,
  `RUNTIME_SESSION_ARTIFACT_ACCOUNT_MAX_BYTES` (5 GiB by default; no
  per-account override yet). A deposit that does not fit evicts the oldest
  unheld artifacts first: recordings, then the other media kinds
  (screenshots, screencasts, audio, generated files, traces), then
  transcripts and documents last, because they are small and are what a
  reviewer searches. An evicted artifact keeps its row, name, labels and
  sha256; only the bytes go, and `availability` becomes `evicted`. When even
  eviction cannot make room the deposit is refused with
  `storage_budget_exhausted`.
- **Screenshots per session.** Browser step screenshots are also bounded per
  session (`RUNTIME_SESSION_SCREENSHOTS_PER_SESSION_MAX`, 500).
- **Legal hold.** An artifact copies the legal hold flag of its session when it
  is stored, and `legal_hold` is in the descriptor. Held artifacts are never
  evicted and never purged.
- **Retention.** Artifacts belong to their session. When the retention purge
  (off by default, `RETENTION_PURGE_ENABLED`) removes a session, its unheld
  artifacts go with it. Bytes removed by a retention policy answer 410 with
  `availability: expired`.

## Audio is off by default

Audio of people is personal data in most jurisdictions. Every `audio` deposit
is refused with `409 artifact_audio_storage_disabled` until the per-account
opt-in setting ships (#1102). Store the transcript instead, with a
`consent_basis` label.

## Standards mapping

Preloop does not invent a content shape. Ingest and tool results are MCP
`ContentBlock`s; export maps to A2A and OpenTelemetry GenAI. Preloop fields
that a standard has no slot for travel in its extension point. The mapping
code is `backend/preloop/services/artifact_shapes.py`.

| Preloop | MCP `ContentBlock` (spec 2026-07-28) | A2A v1.0.1 | OTel GenAI parts (Development) |
| --- | --- | --- | --- |
| text content | `TextContent {type:"text", text}` | `Part {text}` | `BlobPart` with `modality: document` |
| image bytes | `ImageContent {type:"image", data, mimeType}` | `Part {raw, filename, media_type}` | `BlobPart {type:"blob", mime_type, modality:"image", content}` |
| audio bytes | `AudioContent {type:"audio", data, mimeType}` | `Part {raw, filename, media_type}` | `BlobPart` with `modality: audio` |
| any file bytes | `EmbeddedResource {type:"resource", resource:{uri, mimeType, text or blob}}` | `Part {raw, filename, media_type}` | `BlobPart` |
| stored artifact by reference | `ResourceLink {type:"resource_link", uri, name, mimeType, size}` | `Part {url, filename, media_type}` | `UriPart {type:"uri", mime_type, modality, uri}` (preferred for export) |
| JSON segments | `TextContent` or `EmbeddedResource` with `application/json` | `Part {data}` | `BlobPart` with `modality: document` |
| artifact envelope | one block | `Artifact {artifact_id, name, parts[], metadata}` | one part |
| `artifact_id`, `kind`, `labels`, `sha256`, `producer` | `_meta["preloop.dev/artifact"]` | `metadata["preloop.dev/artifact"]` | span attributes `preloop.artifact.kind`, `preloop.artifact.labels` |

Kind to OTel `modality`: `screenshot` is `image`; `recording` and `screencast`
are `video`; `audio` is `audio`; `transcript`, `document`, `generated_file`
and `trace` are `document`. Converting an artifact to any of the three shapes
and back keeps its media type, byte sha256, name, kind and labels.

## Error codes

The REST API returns `{"detail": "<code>"}`; the MCP tool returns a tool error
whose text starts with the code.

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `invalid_content_length` | The `Content-Length` header is not a number. |
| 401 | (authentication error) | No bearer, or an unknown one. |
| 403 | `runtime_session_binding_mismatch` | The key is bound to a different session than the path names. |
| 404 | `runtime_session_not_found` | The session is not in the caller's account. |
| 409 | `artifact_audio_storage_disabled` | Audio deposits are off for the account. |
| 413 | `artifact_too_large` | Larger than the kind cap, or the body is over the request limit. |
| 415 | `artifact_media_type_invalid` | The media type is not allowed for the kind. |
| 415 | `artifact_content_mismatch` | The bytes are not the declared type, or a `generated_file` is an executable. |
| 422 | `artifact_kind_invalid` | Unknown kind. |
| 422 | `artifact_labels_invalid` | A label breaks the rules above. |
| 422 | `artifact_name_invalid` | Missing or too long name (1 to 255 characters). |
| 422 | `artifact_content_required` | No bytes: a multipart call without `file`, or a REST `resource_link`. |
| 422 | `artifact_block_type_unsupported` | The content block type is not one of the accepted MCP types. |
| 422 | `artifact_block_invalid_base64` | `data` or `blob` is not valid base64. |
| 422 | `artifact_request_invalid` | The JSON or the `metadata` part does not match the schema. |
| 422 | `artifact_activity_invalid` | `activity_id` is not a row of this session. |
| 422 | `artifact_parent_invalid` | `parent_artifact_id` is not an artifact of this account. |
| 422 | `artifact_idempotency_key_invalid` | The `Idempotency-Key` header is empty or too long. |
| 422 | `artifact_label_filter_invalid`, `artifact_cursor_invalid`, `artifact_limit_invalid` | Bad list query. |
| 507 | `storage_budget_exhausted` | The account budget cannot fit the artifact even after eviction. |
| (MCP) | `artifact_no_session` | The MCP credential is not bound to a runtime session. |
| (MCP) | `artifact_link_outside_session` | A `resource_link` names an artifact of another session. |
| (MCP) 410 | `artifact_unavailable` | A `resource_link` names an artifact whose bytes were evicted or deleted. |

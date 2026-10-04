# Redaction: what is masked, where, and what is not

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop masks secrets before it logs, stores or forwards data it handles. It
does this with field-name redaction and pattern redaction, applied at
different surfaces. Those two do not remove personal data from free text. A
third pass does, when a `sensitive_data` rule uses the `redact` action:
detected values (email, card numbers, and the other configured types) are
replaced with `[REDACTED:<type>]` on the storage write paths below.
`redact_upstream: true` on that rule also rewrites the copy sent onward.
This page lists each mechanism, where it applies, what stays in clear text,
and how to store less.

## Field-name redaction

`backend/preloop/utils/redaction.py` replaces the value of any dictionary key
that looks sensitive with `***REDACTED***`, recursively through nested
structures. A key matches when, case-insensitively, it is one of
`SENSITIVE_FIELD_NAMES` (`password`, `passwd`, `pwd`, `secret`, `token`,
`api_key`, `apikey`, `api-key`, `auth`, `authorization`, `credential`,
`credentials`, `private_key`, `access_token`, `refresh_token`, `bearer`,
`client_secret`, `webhook_secret`, `jira_webhook_secret`, `device_token`,
`progress_token`, `approval_token`, `key`, `keys` and their unseparated
spellings), starts or ends with one of them, or ends in `token`, `secret`,
`password`, `api_key` or `credential` (optionally plural). So
`github_api_key` and `oauth_client_secret` are masked too.

`redact_dict()` is used before tool arguments are written into approval
records, approval notifications and audit payloads, and `redact_for_log()`
before dictionaries reach application logs.

## Pattern redaction

Free text has no field names, so it is scanned for credential shapes:

- **Model gateway content** (`backend/preloop/services/model_gateway_events.py`):
  PEM private key blocks, `Authorization: Bearer ...` headers, bare
  `bearer <token>` values, `api_key`, `token`, `secret` or `password`
  followed by `=` or `:` and a value, `sk-...` keys, and GitHub `ghp_`,
  `gho_`, `ghu_`, `ghs_`, `ghr_` tokens. Matches become `***REDACTED***`.
- **Flow execution logs** (`backend/preloop/utils/secret_scrubbing.py`):
  every agent log line is scrubbed for known token formats (for example
  GitHub fine-grained and classic tokens), credentials embedded in URLs, and
  secret-looking query parameters. The token prefix is kept
  (`github_pat_[REDACTED]`) so an operator can tell what to rotate.
- **Session search index and browser steps**
  (`backend/preloop/services/session_search_index.py`, `redact_text`):
  transcript messages, tool call summaries, operator notes and browser step
  text are masked for labelled secrets (`api_key: value`, `secret=value`)
  before they are indexed or stored.

## Value-level personal-data redaction

A `sensitive_data` rule with `action: redact` runs the same detectors as a
deny rule, then replaces each match in the stored copy with
`[REDACTED:<type>]`. Keys are left as written. With no matching redact rule,
free-text personal data is stored as it is. The rule's `on` list and scope
(tools, servers, agents) decide which writes are covered.

`redact_upstream` defaults to false: the model provider, the MCP server and
the agent still receive the original text, and only the stored copy is
masked. Set `redact_upstream: true` to rewrite that live copy as well. It
applies to tool arguments sent to the server, tool results returned to the
agent, and model requests sent to the provider. It is rejected on a rule
that does not use `action: redact`, and on a rule whose only target is
`model.response` (a response is masked in storage, not rewritten on the way
back to the client).

## Where each applies

| Surface | Mechanism |
| --- | --- |
| Approval requests and their notifications (email, mobile, Slack, Mattermost, webhook) | Field-name redaction of tool arguments, plus value-level personal-data redaction when a `redact` rule matches |
| Policy-decision rows and tool execution records | Field-name redaction, plus value-level personal-data redaction when a `redact` rule matches |
| Application logs | Field-name redaction via `redact_for_log()` |
| Model gateway events (conversation preview, request and response bodies) | Field-name redaction on payload keys plus pattern redaction on content |
| Gateway usage search text | Pattern redaction, plus value-level personal-data redaction when a `redact` rule matches |
| Flow execution logs | Secret scrubbing of each log line, then value-level personal-data redaction when a `redact` rule matches |
| Session search index, browser steps | Pattern redaction (`redact_text`), plus value-level personal-data redaction when a `redact` rule matches |
| Tool arguments sent to an MCP server, tool results returned to the agent, model requests sent to the provider | Unchanged, unless `redact_upstream: true` on a matching `redact` rule, which rewrites that copy |

## What is not redacted

- **Personal data in free text when no `redact` rule matches.** Names, email
  addresses, postal addresses or customer records inside prompts, completions
  or tool results are stored as they are until a
  [sensitive-data rule](../guide/model-content-policies.md) with
  `action: redact` covers that write. `deny`, `notify` and
  `require_approval` detect the same values but do not rewrite them.
  `redact_upstream` is what changes the copy that leaves Preloop; without it
  the provider, server or agent still sees the original.
- **Secrets that match no pattern and sit under an unremarkable key.** A
  password in a field called `note` is kept.
- **Anything your MCP server or model provider logs on its side.**

## What the model gateway stores

With `MODEL_GATEWAY_CAPTURE_CONTENT=true` (the default), each gateway call
records a conversation preview and the request and response bodies, after the
redaction above. Each preview message is kept up to
`MODEL_GATEWAY_MAX_PREVIEW_CHARS` characters (default 32768, which holds a
typical message in full) and each string in the stored bodies up to
`MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS` (default 8192). In practice this means
most of a conversation is stored, redacted, not only a short snippet. The
event carries its capture policy (`content_capture_enabled`,
`max_preview_chars`, `content_redacted`, `content_truncated`), so a reader can
tell what was kept.

## Storing less

| Setting | Effect |
| --- | --- |
| `MODEL_GATEWAY_CAPTURE_CONTENT=false` | Message content in gateway events is replaced with `***REDACTED***`; usage, cost and metadata are still recorded. Session search then has no gateway content to index. |
| `MODEL_GATEWAY_MAX_PREVIEW_CHARS` | Lower it to keep shorter previews. |
| `MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS` | Lower it to keep shorter request and response bodies. |
| `MODEL_GATEWAY_AUTO_INDEX_INTERACTIONS=false` | Completed gateway interactions are not indexed into the semantic search corpus. |

Related: [Security and privacy](security-privacy.md),
[OWASP Top 10 coverage](owasp-top-10-coverage.md),
[Multi-channel notifications](../guide/approvals/notifications.md),
[AI model gateway](../guide/concepts/model-gateway.md).

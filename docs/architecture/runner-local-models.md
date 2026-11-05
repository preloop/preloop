# Runner-proxied local models

Issue: <https://github.com/preloop/preloop/issues/1833>. Target: 0.17.0.

## Design and endpoint identity

An AI model has `endpoint_type` (`direct`, the existing default, or `runner`),
`runner_id`, and `api_endpoint`. A runner endpoint is an OpenAI-compatible HTTP
base URL ending in `/v1`, bound to an account-owned runner. The initial release
accepts literal loopback addresses and `localhost` only. Private office-network
addresses and authenticated upstreams are deferred. The host runs the CLI
natively, so `http://localhost:11434/v1` reaches host Ollama.

`POST /runners/{runner_id}/models` registers a discovered model with an authenticated
account user and model-management permission. CRUD verifies runner ownership,
normalizes the URL, and creates a gateway-enabled model. Repeating the same
runner, normalized URL and model identifier returns its existing registration,
so the operator can recover a lost native allowlist after approving discovery again. The runner separately
persists the operator-approved model ID, base URL and upstream model identifier.
Registration never grants the backend arbitrary access to the host network.

## Wire protocol, version 1

The existing authenticated runner websocket carries a message pair:

* `model_request`: `request_id`, `operation` (`start`, `next`, `cancel`),
  and, for start, `model_id`, `base_url`, `body` (JSON chat completion).
* `model_response`: `request_id`, `status` (HTTP status), `data` (one JSON
  completion or one SSE line), `done`, and an optional bounded `error` code.

Start performs exactly `POST {registered_base}/chat/completions`. Each `next`
permits one more response frame. Cancel terminates the upstream HTTP request.
No caller-controlled method, path, headers, redirects or query string is allowed.
Request IDs are random and scoped to the authenticated websocket. A response on
another runner or an obsolete connection cannot satisfy the request.

The API process holding the websocket subscribes to a runner/account-specific
Core NATS request subject. Gateway workers send start/next/cancel operations and
receive one response per request. NATS is an internal trusted transport, never
exposed to CLI users. This supports separate API and gateway processes and
multiple API workers without assuming an in-memory socket is globally visible.
Requests are ephemeral, never queued for an offline runner or replayed on reconnect.

## Routing and policy

Model lookup, account authorization, budgets, request policy, context optimization,
response policy and usage/audit recording remain in the gateway. Only provider
I/O goes through the runner. The gateway replaces the public model alias with the
registered upstream model identifier. Direct endpoints retain their existing SSRF
validation; runner URLs are never fetched by backend HTTP clients.

Chat completions support tools and SSE streaming. Responses clients, including
Codex CLI, also work through the gateway's existing chat-completions translation,
for both streaming and nonstreaming requests. The shared translation converts
instructions, conversation/tool history, Codex namespace and freeform tools,
output limits and structured output formats into chat-completions fields. Runner
chat SSE is rendered as Responses events; tool calls regain the client-declared
namespace or freeform shape, and usage retains token/cache/reasoning details.
The local model must support the requested chat-completions features.

Runner routes always use this translation, even with
`meta_data.gateway.responses_api: native`; no native Responses probe is sent.
Raw Responses passthrough, Responses-only hosted tools/state, embeddings, audio,
and provider-specific protocols remain unsupported. The transport still calls
only `/chat/completions` and needs no CLI protocol upgrade.

## Security

Both CRUD and runner validate endpoint shape. Only HTTP loopback origins with an
explicit port and `/v1` path are accepted; credentials, fragments, queries,
redirects and alternate paths are rejected. The native runner must match model ID,
URL and upstream model to its local registration allowlist. No cloud credential
or gateway bearer token crosses the tunnel. Body content is handled like existing
provider traffic and must not appear in diagnostic logs. Registration requires
explicit operator consent. Deletion, credential rotation or websocket replacement
ends that connection's pending requests.

## Back-pressure, limits and failure modes

One response frame is outstanding per request. The runner reads upstream only
when the gateway requests the next frame, keeping buffers bounded and leaving
heartbeats and job traffic responsive. Limits: four active requests per runner,
1 MiB request JSON, 64 KiB SSE line or nonstream completion, 16 MiB total response, 5 second local connect,
60 second idle/first response, and 10 minute overall lifetime. Registration and
discovery inventories are bounded. Slow or abandoned consumers are cancelled;
transport shutdown closes upstream response bodies and releases slots.

No NATS subscriber returns `runner_offline` (503) promptly. Broker loss,
websocket disconnect, malformed frames and missing replies fail with a bounded
503/504 error. Upstream HTTP errors retain status without leaking response bodies.
A failed stream ends with the gateway's existing stream error handling; a partial
stream is never restarted. A disconnected runner does not fall back to direct
backend access or a different model. Runner deletion retains model identity for
history but leaves it unavailable. Reconnect does not resume partial generations.

## Discovery and validation

`preloop runner models` probes Ollama 11434, LM Studio 1234, vLLM 8000, and
llama.cpp 8080 via `/v1/models`. Operators can supply additional loopback base
URLs. Discovery uses short deadlines, caps response bytes/model counts, lists
results, and offers registration during runner setup and via the command.
Noninteractive registration requires an explicit flag.

Tests cover endpoint validation, ownership, registration, offline routing,
streaming frame order, cancellation, bounds and runner allowlist enforcement.
The acceptance run uses compose project `apw-runner`, API 54000, gateway 54001,
console 54173, native runner and host Ollama. Evidence must include streamed tokens
and `[DONE]`, plus the offline error after disconnecting the runner.

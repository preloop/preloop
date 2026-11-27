# Local models through a runner

Use host Ollama, LM Studio, vLLM or llama.cpp through the Preloop gateway without
exposing the model server to the internet. Run the CLI natively on the machine
hosting the model server. Requests retain gateway authorization, policy checks,
budgets and usage records.

1. Start an OpenAI-compatible local server, for example Ollama on port 11434.
2. Log in to your instance and set up the runner with `preloop runner setup`.
   Setup discovers local servers and offers to register their models. Approval
   permits account gateway users to send prompts to those models.
3. Discover later with `preloop runner models`. Register with
   `preloop runner models --register`, then confirm the prompt. For unattended
   setup, explicitly approve with `--register --yes`.
4. Reconnect the runner after registration. For a service use
   `preloop runner restart`; for a foreground runner, stop it and run
   `preloop runner fg` again. The registration command prints each gateway alias.
5. Select that alias in a gateway client using your ordinary gateway API key.

```sh
curl "$PRELOOP_GATEWAY/openai/v1/chat/completions" \
  -H "Authorization: Bearer $PRELOOP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<printed gateway alias>","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

Default discovery probes ports 11434, 1234, 8000 and 8080 at `/v1/models`. Add a
custom loopback endpoint with
`preloop runner models --base-url http://127.0.0.1:9000/v1 --register`.
Discovery and registration require no model-server API key. Authenticated model
servers are outside this release.

For isolated foreground instances, set `PRELOOP_RUNNER_STATE_DIR` to a private
directory before both registration and `runner fg`. This isolates runner identity
and model permissions from your normal runner state.

The runner stores approved model IDs, URLs and upstream identifiers in its private
`runner.json` state. A server request cannot change that allowlist. Removing a
model configuration in the console prevents new gateway routing to it; removing
its local registration and reconnecting also removes host permission. Keep model
servers bound to loopback. Only HTTP loopback `/v1` endpoints are supported in
this release, with redirects and environment proxies disabled.

If a model reports `runner_offline`, start or reconnect its registered runner and
retry. A runner can handle four local generations concurrently. Requests over
1 MiB, SSE lines or nonstream completions over 64 KiB, and responses over 16 MiB
fail with a bounded error. Request time limits are described below.
Canceling a gateway stream cancels its local request. Partial generations do not
resume after a disconnect.

Chat-completions clients and Responses clients such as Codex CLI can use the same
registered gateway alias. For a Codex flow, select the runner model with its
gateway enabled; no direct connection from the backend to the local model server
is needed. Both streaming and nonstreaming `/openai/v1/responses` requests are
translated to chat completions, including Codex MCP namespace tools and freeform
tools. The gateway translates the reply back to Responses events or JSON and
records usage. The local model must support tool calling for agent workflows.

This is a protocol translation, so native Responses passthrough, Responses-only
hosted tools and server-side conversation state are not supported. Embeddings,
audio and provider-specific APIs remain unsupported. Runner routes use chat
translation even if the model's Responses mode is set to `native`. Existing
runner installations need no CLI protocol upgrade for this gateway change.

See the [transport design](../architecture/runner-local-models.md) for routing,
security and failure handling.

## Instance-provided models and local prices

An operator-provisioned global model (`account_id=None`) becomes a built-in
when its gateway is enabled and it is not retired. Its configured gateway alias
is preserved; without one, the provider/model identifier is used. Any provider
can supply a built-in. Set `meta_data.retired=true` or disable the gateway to
retain a historical model without offering it for new work. Public model
responses expose the model name and alias, not its upstream endpoint or secrets.

For directly reachable private model servers, add only their required CIDRs to
`MODEL_ENDPOINT_ALLOWED_CIDRS` on each backend process. Discovery uses the same
operator policy as inference. Discovery checks all DNS answers, pins its
connection to a checked address, preserves the original TLS hostname, and
refuses redirects. Link-local metadata, unspecified and multicast destinations
remain blocked even if a broad CIDR is configured.

Local inference defaults to $0 provider cost. Explicit account or model prices
still take precedence, for example to account for hardware costs. This default
applies to local-provider defaults (Ollama or LM Studio), explicitly allowed
private literal endpoints, and models explicitly marked
`meta_data.local_inference=true`. Use the explicit mark for local hostname
endpoints; pricing does not resolve DNS. Models registered through a personal
runner also default to $0 without requiring a backend CIDR allowlist; explicit
prices and account overrides still take precedence. An unknown remote OpenAI-compatible
endpoint remains unpriced unless a price is supplied. The local default is
reported as model configuration, not a fetched provider tariff.


Database initialization during upgrades preserves configured gateway aliases,
explicit URLs, disabled models and retired models. An omitted gateway URL stays
omitted, allowing runtime environment settings to select the internal service
address. The initializer does not copy the public `PRELOOP_URL` into model routes. Legacy
duplicate aliases do not block upgrades; normal model edits still enforce alias
uniqueness.


## Long model requests

A flow's execution timeout and a single model request's timeout are separate.
The server sends the effective request limits to the runner; the CLI needs no
additional configuration. Configure these environment variables on the API,
gateway and workers, then restart those services:

| Setting | Default | Meaning |
| --- | --- | --- |
| `RUNNER_MODEL_IDLE_SECONDS` | `60` | Runner liveness: no request frames or keepalives. Minimum `30`. |
| `RUNNER_MODEL_SILENT_SECONDS` | `600` | Local model silence, before response headers and between response bytes. |
| `RUNNER_MODEL_TOTAL_SECONDS` | `3600` | Total time for one model request, including time waiting for its consumer. |

The runner sends a small keepalive every 10 seconds while a request is active.
Keepalives reset runner liveness; they do not reset the model silence or total
limits. Byte progress reported with a keepalive can reset model silence, including
partial SSE lines and nonstreaming responses. The gateway never exposes these
transport frames as model output. The total is also capped by the calling flow's
remaining execution budget, including startup and work before a park/resume.
Raising the model limits cannot give a request more time than its flow has left.

For example, allow 20 minutes of model silence and two hours per request:

```sh
RUNNER_MODEL_SILENT_SECONDS=1200
RUNNER_MODEL_TOTAL_SECONDS=7200
```

In the OSS Helm chart, use:

```yaml
runnerModels:
  idleSeconds: 60
  silentSeconds: 1200
  totalSeconds: 7200
api:
  terminationGracePeriodSeconds: 7230
gateway:
  terminationGracePeriodSeconds: 7230
```

Helm supplies the same settings to API, gateway and workers and automatically
raises the ingress and console gateway read/send timeouts to at least the total
plus 15 seconds. Explicit ingress annotations still override these defaults;
remove a shorter override or increase it. For Docker/installer or custom reverse
proxies, increase every gateway route's `proxy_read_timeout` and
`proxy_send_timeout` to at least the total plus 15 seconds (defaults: `3615s`).
For rollouts to drain a full request, also keep API and gateway
`terminationGracePeriodSeconds` above the total plus 30 seconds (default `3630`),
as shown in the example. This includes the preStop sleep.
Apply equivalent settings to any additional load balancer or client in the path.
A nonstreaming completion can produce no HTTP response until the full request
finishes, even when the local model is making progress.

Hosted Pi and DeepSeek Harness flows use the flow budget plus 15 seconds for
SDK and HTTP client waits. This setup travels in the server-generated launch
payload, including with existing harness images/plugins. Other clients must allow
at least the effective request budget; a client
with its own shorter timeout can still cancel the request.

Timeouts retain the `runner_proxy_timeout` error code and identify the limit:

- `runner sent no frames for 60 s (RUNNER_MODEL_IDLE_SECONDS=60)`: the request's
  runner stopped responding. Check the runner process and connection.
- `local model sent no data for 600 s (RUNNER_MODEL_SILENT_SECONDS=600)`: the
  runner is alive but the local model stopped producing data. Increase the
  silence limit if loading or evaluating the prompt needs more time.
- `model request exceeded 3600 s`: the request used its total budget. The
  effective value can be lower when the flow has less time remaining.

Cancellation, timeout and disconnect stop local HTTP work and release its slot.
The four-request concurrency limit remains unchanged. Even an abandoned request
that receives no further pulls expires at its own total deadline.

Upgrade both server and CLI to use keepalives and longer local waits. An old CLI
ignores the new fields and retains its 60-second silence and 600-second total
limits. A new CLI with an old server uses those same legacy defaults and sends
no new keepalive frames. A server-only upgrade improves server limits, deadline
accounting, proxy settings and timeout messages; it cannot remove an old CLI's
local limits.

For Ollama, loading a model counts against the first-response silence limit.
Keep it resident with `OLLAMA_KEEP_ALIVE` on the Ollama server (for example,
`OLLAMA_KEEP_ALIVE=24h`), or the native Ollama API's `keep_alive` option, and
preload it before starting the flow. Size the silence budget for prompt evaluation
as well as loading; keeping the model resident only removes the loading delay.

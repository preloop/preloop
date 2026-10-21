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
1 MiB, SSE lines or nonstream completions over 64 KiB, responses over 16 MiB,
60 seconds without progress, or ten minutes total fail with a bounded error.
Canceling a gateway stream cancels its local request. Partial generations do not
resume after a disconnect. Chat completions support streaming and tools;
embeddings, audio and provider-specific APIs are outside this release.

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

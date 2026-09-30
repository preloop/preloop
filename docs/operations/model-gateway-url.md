# Which gateway URL an agent calls

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Every agent that routes model traffic through Preloop is told one base URL,
and a wrong one fails the same way each time: `404 Not Found` from
`POST .../openai/v1/responses`, no upstream call, no tokens billed, a flow
execution that ends with nothing useful in it. The URL is chosen in two
places, and they answer different questions.

## Where the value comes from

A model row may carry its own URL in `meta_data.gateway.url`. `preloop agents
onboard` writes it, and it wins over everything below. The console never
writes that field, and since it also preserves whatever is already there, a
row onboarded by the CLI keeps its URL when someone later edits the model or
re-enables gateway routing.

When the row has no URL,
`preloop.services.model_runtime_resolver.default_model_gateway_url()` picks
one, highest precedence first:

| Source | Meaning |
| --- | --- |
| `PRELOOP_MODEL_GATEWAY_URL` | An explicit URL for any environment. Set it and nothing else applies |
| `PRELOOP_MODEL_GATEWAY_URL_K8S` | The in-cluster URL. The chart renders this on the API and worker pods |
| `PRELOOP_API_SERVICE_HTTP_ENDPOINT` under Kubernetes | The API Service, with the gateway Service substituted (below) |
| `http://host.docker.internal:8000/openai/v1` | The compose and single-node default |

## In a Kubernetes deployment: the gateway Service, not the API Service

The chart splits the image by role. API pods run
`PRELOOP_SERVICE_ROLE=api` and never mount `/openai/v1`; the gateway pods
run `PRELOOP_SERVICE_ROLE=gateway` and serve nothing else. So
`http://<release>-api:80/openai/v1` is a 404 by construction, and agent Jobs
pointed at it fail on their first model call.

The chart therefore sets `PRELOOP_MODEL_GATEWAY_URL_K8S` on the API
Deployment and on every worker pool, which are the pods that launch agent
Jobs. By default it renders `http://<release>-gateway:80/openai/v1`; a
deployment whose gateway lives elsewhere sets `gateway.inClusterUrl` in
values. As a backstop for pods that predate the env var, the resolver swaps a
trailing `-api` in the API Service host for `-gateway` before appending
`/openai/v1`. That swap is skipped only when `PRELOOP_SERVICE_ROLE` is
explicitly `all`, which means one process is serving both surfaces and the
API Service really does answer gateway routes.

Checking a live deployment:

```sh
kubectl set env deploy/<release>-api --list | grep MODEL_GATEWAY
kubectl get svc <release>-gateway
```

## For a runner: the public URL

A self-hosted runner (`preloop runner`, and the ephemeral runner behind the
`run-flow` action) executes outside the cluster and cannot resolve any
in-cluster Service name. `preloop.agents.runner_launch` overrides the
resolved URL with `${PRELOOP_URL}/openai/v1`, expanded by the runner from the
control-plane origin it is already authenticated against, so the same model
row works in both places without a per-row URL.

The two paths in one line: agent Jobs get the in-cluster gateway Service,
runners get the public origin, and an explicit `meta_data.gateway.url`
overrides both.

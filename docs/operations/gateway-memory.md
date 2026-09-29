# Gateway memory: what a concurrent large response costs

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Gateway replicas were OOMKilled while three agents called
`/openai/v1/responses` at the same time (issue #670). Losing a replica
mid-burst turns one account's spike into failed model calls for everyone on
it, so the sizing had to come from a measurement rather than from a guess.

This page records the measurement, how to repeat it, and the reasoning
behind the requests and limits in `helm/preloop/values.yaml`.

## What the gateway process holds

Per in-flight call, for the duration of the call:

- the request body, parsed once by the endpoint,
- the upstream response body, parsed once,
- the usage row and the event payloads derived from both.

Per pass, for the duration of the pass, a background sweeper holds a batch
of rows for every account it visits. Two independent working sets sharing
one memory limit is what turned a normal burst into an OOMKill, so a
dedicated gateway process now runs no background passes at all
(`preloop.services.service_roles`, enforced in each sweeper's `start()`).
A split deploy therefore needs at least one `api` or `all` process:
gateway-only pods will not seal the audit chain, purge retention, or run
optimization jobs.

## Measurement

`scripts/measure_gateway_index_memory.py` measures the indexing work the
response path used to do, against the same work as it is done now, for the
same payloads. `tracemalloc` peak is the attribution number; maximum RSS is
reported alongside it as a sanity check and is inflated by tracemalloc's own
bookkeeping, so treat it as directional.

```bash
PRELOOP_DISABLE_TELEMETRY=true \
  python3 scripts/measure_gateway_index_memory.py --concurrency 3 --payload-kb 1024

# with a background pass running in the same process, on a disposable database
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/preloop \
PRELOOP_DISABLE_TELEMETRY=true \
  python3 scripts/measure_gateway_index_memory.py --concurrency 3 --payload-kb 1024 \
    --seed-accounts 186 --seed-audit-rows 200 --with-seal-pass --seal-lag-seconds 0
```

Results, 1 MiB prompt plus 1 MiB answer per response, Python 3.14, one
laptop process, 2026-09-15:

| concurrency | indexing | background pass | peak traced | per response |
|---|---|---|---|---|
| 1 | before (copy then flatten) | no | 10.05 MiB | 10.05 MiB |
| 3 | before (copy then flatten) | no | 28.16 MiB | 9.39 MiB |
| 5 | before (copy then flatten) | no | 46.25 MiB | 9.25 MiB |
| 3 | now (bounded document) | no | 0.04 MiB | 0.01 MiB |
| 3 | now (bounded document) | audit seal pass, 186 accounts, 37200 rows | 4.47 MiB | - |

Reading it:

- Indexing used to cost about 9 MiB for every 1 MiB response, roughly nine
  times the payload, and it scaled linearly with concurrency. Two effects:
  a sanitized deep copy of both payloads, then whitespace-normalizing every
  value in full (`" ".join(str(value).split())` on a megabyte of text
  materialises every word in it) before keeping the first 2000 characters.
- Indexing now costs about 0.01 MiB per response: the payloads are read
  once into a document bounded by 256 lines and 16000 characters, and the
  database write happens on a queue worker with its own session. The cost
  no longer depends on payload size or on concurrency.
- One audit seal pass over 186 accounts and 37200 unsealed rows allocates
  about 4.5 MiB while it runs, and about 2.4 MiB when it finds nothing to
  seal. Small on its own; it is memory the pod does not need to spend at
  all, and it arrives on a schedule that is unrelated to the traffic it
  would be competing with.

Not measured here, because it needs a cluster: the resident cost of a full
replica under sustained streaming traffic. `scripts/capacity/` is the
harness for that, and the requests below still come from observed idle RSS
on hosted clusters plus the per-response numbers above.

## Requests and limits

`helm/preloop/values.yaml`, `gateway.resources`:

- Request 768Mi. Observed idle RSS on hosted clusters is 630-700Mi
  (LiteLLM, the price catalog and uvicorn), so a request below that made
  the HPA read ~250% memory utilization while CPU was almost idle and
  pinned the deployment at `maxReplicas`. Extra replicas each pay the same
  idle RSS, which makes the cluster worse, not better.
- Limit 2Gi. Headroom above the request is 1.25Gi. A large interaction now
  costs its own payloads (single-digit MiB each, request plus response)
  plus ~0.01 MiB of indexing, so 1.25Gi covers far more than the three
  concurrent large responses that OOMKilled a 1Gi replica, and leaves room
  for capture buffers on streaming calls.
- The 256Mi request and 1Gi limit named in issue #670 are what the incident
  ran with; they were already raised before this change.

If these numbers are revisited, state the measurement next to the values,
the way the comment in `values.yaml` does.

## Operator knobs

The off-path indexing queue is bounded so a backed-up writer cannot grow
memory. Both knobs are environment variables, read through
`preloop.config.settings`.

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_USAGE_INDEX_QUEUE_MAX_PENDING` | 256 | Documents waiting to be written. New ones are dropped when the queue is full. |
| `GATEWAY_USAGE_INDEX_QUEUE_ENABLED` | true | Whether the process may start the writer thread. `TESTING=true` always disables it. |

A full queue is the correct failure: the corpus is an opt-in convenience,
and memory pressure is the problem this exists to avoid.

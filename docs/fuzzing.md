# Continuous fuzzing

Fuzzing generates and mutates inputs to find crashes, hangs, and violated
properties. The native Go fuzzer uses coverage feedback to keep mutations that
reach new code. Our [workflow](../.github/workflows/fuzz.yml) runs it on relevant
pull requests and nightly, with two production parsers:

| Target | Real input surface | Properties checked |
| --- | --- | --- |
| `FuzzMCPEventStream` | CLI replies from MCP servers, including SSE progress messages | No panic; identical bytes decode to the same response and progress across transport read boundaries |
| `FuzzCanonicalJSON` | JSON from downloaded audit/evidence payloads used for hashing | Exactly one valid JSON document; canonical bytes remain valid, preserve decoded values, and are idempotent |

Both targets run in memory. They do not start Preloop, connect to databases,
launch agents, or call model providers. `PRELOOP_DISABLE_TELEMETRY=true` is set.
Fuzzing checks malformed inputs and invariants. A stress test instead measures
capacity, latency, failure rates and recovery under concurrent traffic; neither
substitutes for the other or proves the absence of vulnerabilities.

## Budgets and safety

- Relevant PRs: 60 seconds of mutation per target, two workers per job.
- Nightly and manual runs: 600 seconds per target, two workers per job.
- Two matrix jobs maximum; job timeout 20 minutes, Go test timeout 15 minutes,
  and failure minimization capped at 20 seconds.
- Inputs larger than 64 KiB are skipped. This bounds normal test cost; it does
  not test arbitrarily large streams or provide a production input-size limit.
- `GOMEMLIMIT=512MiB` is a soft Go runtime limit per process, not a container
  memory ceiling. Two worker processes plus the coordinator can use more.
- Mutation time totals two runner minutes per PR and twenty nightly, before
  build/setup overhead. GitHub Actions billing depends on repository/runner
  eligibility; there is no model inference cost.

All actions are pinned to commit SHAs. Go comes from the checked-in module and
toolchain versions, and module contents are checked against `go.sum`. Jobs have
only `contents: read`, checkout does not retain credentials, and no secrets are
passed to fuzz commands. The workflow uses `pull_request`, never
`pull_request_target`. Maintainer approval for fork workflows still follows the
repository's GitHub settings.

Only successful runs on `main` save the coverage corpus cache. PRs can restore
that corpus but cannot promote their generated corpus into the default branch
cache. Cache keys include target, OS and Go manifests. The checked-in seed
corpus works independently of cache availability or eviction.

## Run and reproduce locally

From the repository root, use the Go version selected by `cli/go.mod`:

```bash
cd cli
export PRELOOP_DISABLE_TELEMETRY=true
export GOMAXPROCS=2
export GOMEMLIMIT=512MiB
# Runs ordinary tests and every checked-in fuzz seed deterministically.
go test ./internal/mcpclient ./internal/verify
# Mutates one target for a bounded local run.
go test ./internal/mcpclient -run '^$' -fuzz '^FuzzMCPEventStream$' \
  -fuzztime 30s -fuzzminimizetime 20s -parallel 2 -timeout 2m
go test ./internal/verify -run '^$' -fuzz '^FuzzCanonicalJSON$' \
  -fuzztime 30s -fuzzminimizetime 20s -parallel 2 -timeout 2m
```

The standard Go test corpus format lives in each package's
`testdata/fuzz/<Target>/` directory. Seeds include valid messages, progress-only
streams, malformed/truncated JSON, control characters, large literal numbers,
duplicate keys and concatenated documents. `go test ./...` replays them without
starting a mutation campaign.

When a property fails, Go prints the reproduction command and writes the
minimized input to `testdata/fuzz/<Target>/<hash>`. Workflow artifacts retain
these files and logs for 30 days even after a failed run. Download the artifact
and place the failing input in that same directory in a local checkout, then:

```bash
go test ./internal/verify -run 'FuzzCanonicalJSON/<hash>' -count=1
```

Use the target/package printed in the failure. Inspect an input before sharing
it publicly. Investigate the property and production behavior, fix the defect
without suppressing the assertion, and commit the minimized input alongside the
fix so normal tests prevent a recurrence. If a bug is security-sensitive, follow
[SECURITY.md](../SECURITY.md) instead of opening a public disclosure prematurely.
Coverage corpus entries are useful future seeds, not failures to file as bugs.

The initial single-document property found that canonical decoding silently
ignored trailing JSON. The concatenated-documents regression now requires
rejection, while valid trailing whitespace and existing backend signature/hash
fixtures continue to pass. Rendering and cryptographic algorithms are unchanged.

## Scorecard

OpenSSF Scorecard v5.5.0 recognizes native Go `Fuzz` functions in `_test.go`
files ([detection code](https://github.com/ossf/scorecard/blob/c395761d/checks/raw/fuzzing.go)).
No ClusterFuzzLite container or OSS-Fuzz enrollment is required for these targets.
Presence detection does not demonstrate adequate coverage or that a campaign ran;
use workflow logs, retained regressions, and observed executions as evidence.
Published Scorecard results can lag after merging. Do not claim a score increase
until a fresh published result reports it.

Official references: [Go fuzzing](https://go.dev/doc/security/fuzz/),
[Go fuzzing tutorial](https://go.dev/doc/tutorial/fuzz), and
[ClusterFuzzLite Go integration](https://google.github.io/clusterfuzzlite/build-integration/go-lang/)
for projects that later need its managed libFuzzer infrastructure.

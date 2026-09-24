# Execution environments and checkpoint recovery

Default images are generic harness/toolchain images. They do not bundle the
Preloop application, PostgreSQL, or another customer's application. A project can
supply its own image and dependencies. An optional hosted environment profile
supplies the application dependencies an implementation agent needs before it
spends its model budget. An administrator installs a
JSON registry and sets `FLOW_ENVIRONMENT_PROFILES_FILE` to its path. A flow
selects an entry with `agent_config.environment_profile`. Issue descriptions
cannot add images, mounts, host privileges or secrets to that registry.

Profiles require an image digest, harness (`codex` or `opencode`) and protocol
version 1. The image contains Python 3.12+, the harness, and
`/opt/preloop-environment.json` with `version: 1` and the harness name. Startup
rejects an incompatible image before setup. Source still comes from the
requested repository checkout. The profile image does not include a stale
copy of the application.

`environments/preloop/Dockerfile` is a Preloop-specific example and integration
fixture, not a default agent image. It extends the existing `Dockerfile.dev` image
with pinned Codex, Playwright/Chromium and a PostgreSQL Python driver. Build
from the repository root so the hash-pinned `tools/` lockfile and
`requirements.txt` copy in. Build the dev image first, then pass its digest as
`DEV_IMAGE`. Register the resulting image digest. `environments/preloop/profile.json.example` contains
component, backend and full-application profiles; replace the explicit digest placeholders with
operator-approved digests. The component profile only installs frontend
packages. The backend profile starts disposable PostgreSQL/pgvector and NATS.
Neither starts the complete application for every change. The application profile explicitly calls `scripts/flow-environment-app-check.sh`,
which starts and health-checks the API/frontend, seeds disposable test data and
cleans up its child process groups.

Each dependency has a pinned image, unique name/port, optional command and
nonproduction environment variables. Docker provisions an execution-specific
network with service DNS aliases. Kubernetes uses native sidecars (Kubernetes
1.29+ with sidecars enabled), localhost ports and startup probes; the kubelet
terminates them when the main container ends. Services are also removed during
executor cleanup. No host Docker socket is mounted. Private runners currently
reject named version-1 profiles explicitly because their custom image entrypoint
contract differs. Existing private custom images remain supported; they receive
`GIT_CLONE_CONFIG` and `CUSTOM_COMMANDS` JSON, including setup commands. Those
images must implement these entrypoint fields themselves.

A private-runner flow can select its project image directly through the flow API:
`agent_config: {"image": "registry.example.com/team/project-agent:release"}`.
The private launch log reports the effective image reference, including either
custom-image alias, alongside the installed harness version. The reference is
the requested tag or digest; a tag alone does not attest the pulled image digest.
No named profile registry is required. `docker_image` is also accepted; when both
keys are present the runner checks `image` first. Omit `environment_profile` for
this raw image path. An explicit nonempty override takes precedence over the
operator's per-harness image environment variable and the generic fallback.
Docker on the private host must be able to pull that image, whose entrypoint must
consume the flow environment contract. The flow form offers a **Custom container
image** field for ephemeral execution on a private runner (an explicit runner or
label, or a private account default). It loads `image` first and falls back to
`docker_image`, writes a trimmed `image` on save, and keeps the saved value
untouched while the field is unavailable (hosted, Auto, persistent, or native
host execution), so an override can still be managed through the API.

Setup has its own timeout and failure marker, with output under
`/workspace/evidence/setup.log`. Readiness runs on every attempt. Profiles may
list lockfiles and cache paths; dependency setup is reused only when the profile
and lockfile contents match and all declared cache paths still exist. Restoring
a workspace without its reproducible dependencies forces setup to run again.
Image layers remain reusable independently. Test-command groups and artifact
paths describe the repository's verification contract; the publication gate
chooses and verifies the relevant commands. Issue readiness requires an enabled
verification gate. Every command in its always/rule/unknown-impact policy must
have an environment command group with the same ID and exact shell text (multiple
steps are joined with newlines). Issue acceptance command IDs must also appear
in the verification policy. Capability readiness is not test-result attestation;
agent-sandbox files and log markers cannot authorize isolated publication.

## Browser profile

`preloop-browser` in `environments/preloop/profile.json.example` uses the same
image as the component profile and adds an `egress-proxy` sidecar. The proxy
contract (listen port, `EGRESS_ALLOWED_ORIGINS`, `EGRESS_ALLOW_PRIVATE_CIDRS`,
`EGRESS_DENY_PRIVATE`, and `GET /healthz`) is the one in
`environments/egress-proxy/README.md`. `DependencyService.env` already stores
that service environment on the registered profile. A flow only selects the
profile identifier, so the origin allowlist is fixed per profile rather than
copied from `agent_config`.

`environments/preloop/browser/enable.sh` reads `PRELOOP_BROWSER_PROXY`
(`http://egress-proxy:3128` on Docker, `http://127.0.0.1:3128` on Kubernetes),
renders `playwright-mcp.config.json`, and registers a `browser` MCP server for
`PRELOOP_HARNESS` (`codex` or `claude`). Codex reads
`~/.codex/config.toml` (`[mcp_servers.browser]` with `command` and `args`).
The hosted Codex executor writes `[mcp_servers.preloop]` from
`backend/preloop/agents/codex.py` and would replace that file; setup also
leaves `~/.codex/preloop-browser-mcp.toml`, which the executor appends after
its own write. There is no Claude Code writer in this repository; Claude Code
reads `.mcp.json` in the checkout, and `enable.sh` merges the `browser` entry
there. The pinned package is `@playwright/mcp@0.0.82`, installed in the profile
image next to Playwright `1.64.0-alpha-1789764292000` (the build that package
bundles). The MCP command is the image binary
`/opt/preloop-env-tools/node_modules/.bin/playwright-mcp` with `--config`,
`--proxy-server`, `--isolated`, and `--headless`. Setup does not fetch the
package from the npm registry. `--isolated` starts Chromium with an empty
profile: no cookies and no operator storage state. The rendered launch args
also include `--proxy-bypass-list=<-loopback>` so loopback is not a path
around the proxy. The self-check probes the metadata address, a
non-allowlisted origin, and a listener on `127.0.0.1`.

The example profile does not list `test_commands`. A flow that gates
verification on command IDs must add those IDs to the registered profile;
otherwise readiness reports `environment_command_missing`.

Chromium is started with the two flags the proxy README requires:
`--proxy-server` and `--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE
<proxy host>`. Without them the browser can open connections that never pass
the allowlist. `--no-sandbox` is added only when the probe runs as uid 0.

The proxy enforces origins. The harness MCP entry and Preloop tool permissions
decide which tools the agent may call. Tool selection is not a network
boundary. `enable.sh` then runs `selfcheck.sh`, which requires
`$PRELOOP_BROWSER_PROXY/healthz` to answer `ok` and launches Chromium once
against `http://169.254.169.254/` and `https://example.org/`. Both probes must
fail with a proxy error (`egress_denied` or a Chromium proxy/tunnel error).
Any other outcome, including a missing proxy variable, a failed health check,
or a probe that loads, exits `browser_egress_not_enforced` and aborts profile
setup.

Routing this browser MCP server through the Preloop firewall is a follow-up.
The control plane cannot reach the execution network to sit on that path
today. Issue #885 would then add timeline rows for those tool calls.

## Durable hosted artifacts

Enable `FLOW_ARTIFACT_DIRECT_UPLOAD` when the runner can reach `PRELOOP_URL`.
The [checkpoint Helm overlay](../../../helm/preloop/values-native-checkpoints.yaml)
enables direct uploads with a 64 MiB compressed cap and matching 80 MiB proxy
limits. Merge its `extraEnv` entries with existing installation values.
Without it, the legacy snapshot path remains in effect, including the 2 MiB
Kubernetes log-channel cap. Raising `WORKSPACE_SNAPSHOT_MAX_BYTES` alone does
not raise that log cap. `FLOW_EVIDENCE_LOG_PLAINTEXT=false` refuses that log
channel (the snapshot is then skipped with `plaintext_disabled`); see
[evidence storage](evidence-storage.md). A skipped legacy snapshot does not
mean setup failed.
With direct upload enabled, workspace
checkpoints travel through authenticated HTTP, never the pod log channel.
The service validates compressed and expanded size, archive paths and file
kinds, encrypts the payload with the configured encryption key, and commits the
immutable manifest and payload together in PostgreSQL. An interrupted upload
cannot become the latest checkpoint. Evidence packs use the same store with
`kind=evidence` and a separate retention window; see
[Evidence storage and retention](evidence-storage.md). Workspace checkpoint
log transport is skipped when a checkpoint PUT token is present.

Capabilities permit one artifact kind and operation for one execution, account,
flow and implementation thread. They confer no general storage access. Reads
check the persisted thread binding, manifest identity and payload digest.
Native-session archives use their own artifact kind and capability;
`FLOW_NATIVE_SESSION_RETENTION_HOURS` (168 by default) caps their manifest expiry
independently of workspace retention, so either artifact can report its own expiry; workspace
capture excludes session directories. Agent containers never receive the
control plane's encryption key. Use a dedicated `SECURITY__ENCRYPTION_KEY` in
production, protect it separately from the database, and retain it across
restarts. Key rotation must preserve access to artifacts encrypted by old keys.

A five-minute loop captures source and git state. It checks file sizes,
modification times and membership for changes during capture, declining a busy
snapshot rather than committing inconsistent state. The last completed
checkpoint survives process/pod loss; writes after that checkpoint can be lost.
Controlled exits attempt a final checkpoint. Before legacy wrapper publication,
a failed checkpoint blocks publication, except when the archive exceeds the
storage cap: that case logs `PRELOOP_CHECKPOINT skipped checkpoint_oversized`,
exits 0, and leaves the last completed checkpoint as the resume point. A
trusted external publisher must make this checkpoint barrier part of its
handoff as well.

Restore occurs before setup or agent startup on Docker and Kubernetes. It logs
the age of the checkpoint it recovered (`PRELOOP_CHECKPOINT restored
age_seconds=... created_at=...`), so work lost to node loss is visible rather
than assumed to be zero. Source, each repository's branch, head and upstream
base commit, staged/unstaged edits and required untracked files are retained.
Reproducible dependencies, known credential locations, environment files,
symlinks, git configuration (including submodule and worktree copies, which
carry the same remote URL credential) and native session directories are
excluded. This exclusion list is not a content-level secret detector; keep
production credentials out of implementation workspaces. The trusted clone
configuration recreates remotes. Divergent/newer remote commits are detected
without checking out or overwriting the restored local branch. A restored
branch that has never been pushed can continue when the remote confirms that
the branch is absent. Authentication/network errors remain explicit
`remote_unavailable` failures rather than being confused with divergence or
absence. A branch identity mismatch blocks recovery. Missing, corrupt
or expired checkpoints fail resume explicitly. Automatic cold branch fallback
is disabled because a remote branch does not prove unpublished local work was
preserved. Private executors never receive hosted artifact capabilities.

`WORKSPACE_SNAPSHOT_MAX_BYTES` bounds compressed uploads;
`FLOW_ARTIFACT_EXPANDED_MAX_BYTES` bounds extraction;
`FLOW_ARTIFACT_ACCOUNT_QUOTA_BYTES` bounds retained encrypted account payloads.
Retention uses `WORKSPACE_SNAPSHOT_TTL_HOURS`; zero expires on the next cleanup
pass. Downloads take a lease so cleanup cannot remove their payload during
restore. Metadata remains with availability `expired` after bytes are removed.
Use retention appropriate to the review window, and surface expiry instead of
claiming a resumable session remains available.

Private workspaces remain in the runner's local configuration directory with
restricted directory/file permissions. Operators are responsible for host disk
encryption. `PRELOOP_RUNNER_WORKSPACE_MAX_BYTES` bounds retained workspace bytes
(default 4 GiB); oldest unleased directories are removed first and a local
`.expired` tombstone distinguishes expiry/quota loss from a missing runner.
`PRELOOP_RUNNER_WORKSPACE_TTL_HOURS=0` disables retention; active jobs are
protected. Cleanup also runs during idle heartbeats. No private workspace is
uploaded by this transport. A continuation of a persisted workspace is leased
only to the runner that holds it: while that runner is offline the execution
stays queued with a message naming it, and after the queue deadline it fails
for operator action rather than moving to another host. If a job does reach a
runner without the workspace it was told to resume, the runner refuses it
(`workspace_recovery_unavailable`) instead of cloning cold and dropping the
unpublished work; the local `.expired` tombstone distinguishes retention or
quota loss from a wrong host.

## Validation

The repository includes archive security, tenant/thread isolation, encrypted
roundtrip, lease/expiry, setup timeout, cache invalidation and private runner
contract tests. `scripts/tests/flow_environment_integration.py` runs real SQL and
Chromium checks through the hosted executor, checkpoints through HTTP, kills
the sandbox, and checks exact unpushed/dirty/untracked recovery in a new one.
Supply a migrated disposable `DATABASE_URL`, image digests and, for Kubernetes,
an explicit disposable `--kubeconfig`. It never invokes a model. Set
`PRELOOP_DISABLE_TELEMETRY=true` for all such tests and setup scripts.


This fixture verifies the generic runtime and recovery contract. The SQL/browser
probe is an optional example, not a required application stack or a claim that a
project's full end-to-end suite passes. Run the repository's relevant verification
commands separately; missing dependencies and unavailable checks remain explicit
blocked evidence. Use immutable `repository@sha256:<digest>` references for both
profile and service images. Raw private-runner image overrides do not require a
named profile registry; named private profiles remain unsupported until the
runner advertises that protocol.

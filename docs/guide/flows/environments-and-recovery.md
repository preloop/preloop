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
with pinned Codex, Playwright/Chromium and a PostgreSQL Python driver. Build the
dev image first, then pass its immutable reference as `DEV_IMAGE`. Register the
resulting image digest. `environments/preloop/profile.json.example` contains
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
No named profile registry is required. `docker_image` is also accepted; when both
keys are present the runner checks `image` first. Omit `environment_profile` for
this raw image path. An explicit nonempty override takes precedence over the
operator's per-harness image environment variable and the generic fallback.
Docker on the private host must be able to pull that image, whose entrypoint must
consume the flow environment contract. The flow form currently has no dedicated
image-override field; configure this through the API.

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

## Durable hosted artifacts

Enable `FLOW_ARTIFACT_DIRECT_UPLOAD` when the runner can reach `PRELOOP_URL`.
Without it, the legacy snapshot path remains in effect. With it, workspace
checkpoints travel through authenticated HTTP, never the pod log channel.
The service validates compressed and expanded size, archive paths and file
kinds, encrypts the payload with the configured encryption key, and commits the
immutable manifest and payload together in PostgreSQL. An interrupted upload
cannot become the latest checkpoint. Result/evidence log transport is unchanged;
this does not claim to finish the separate evidence-hardening work in #268.

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
a failed checkpoint blocks publication. A trusted external publisher must make
this checkpoint barrier part of its handoff as well.

Restore occurs before setup or agent startup on Docker and Kubernetes. Source,
commits, staged/unstaged edits and required untracked files are retained.
Reproducible dependencies, known credential locations, environment files,
symlinks, git credential configuration and native session directories are
excluded. This exclusion list is not a content-level secret detector; keep
production credentials out of implementation workspaces. The trusted clone
configuration recreates remotes. Divergent/newer remote commits are detected
without checking out or overwriting the restored local branch. Missing, corrupt
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
uploaded by this transport. Continuation must stay on its owning runner; a
missing/offline owner is an operator-visible scheduling constraint.

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

# Rehearsing an upgrade locally

Editions: OSS. This gate exercises the published Docker Compose service definitions
and Helm charts with disposable synthetic data. It never targets an existing
installation or Kubernetes context. It supports [the 1.0 upgrade gate](https://github.com/preloop/preloop/issues/1869).

Use `--baseline v1.0.2` to select the published 1.0.2 release. Without that
option, the runner retains discovery of the highest stable **published** 0.17.x
release using the GitHub releases API. Drafts, prereleases, and unpaired tags are ineligible. It
exports that tag's source, pulls the published backend image, and checks its OCI
source revision against the tag commit. The candidate is either a local image
built from an exact committed tree or the image for an actually published RC.
Every report records commits, image IDs, registry digests, platform, migration
heads, validation results, and cleanup results.

## Prerequisites and candidate rehearsal

Use a disposable Linux Docker host or Docker Desktop with Linux containers,
Python 3.12+, Docker Engine 28+ with Compose v2, Git, authenticated `gh` with public repository
read access, Helm 3, `kubectl`, and kind. The CI recipe pins kind v0.30.0 and
Helm v3.19.0. Compose-only runs do not need the Kubernetes tools. `--cli-matrix`
also needs Go (see `cli/go.mod`) and make; its Linux binary is built with CGO
disabled for the Docker host architecture. The runner creates a single local kind node; it never accesses
Minikube, a pre-existing cluster, or a remote database. Runtime resource limits
live in `scripts/upgrade_acceptance/runner.py` and `helm.py`.

From a checkout containing the harness:

```bash
export PRELOOP_DISABLE_TELEMETRY=true
python3 -m venv .venv-upgrade
. .venv-upgrade/bin/activate
python -m pip install PyYAML==6.0.3 pytest==9.1.1
python -m pytest scripts/upgrade_acceptance/tests -q
git fetch origin main --tags
candidate_commit=$(git rev-parse origin/main)
python scripts/upgrade_acceptance/runner.py \
  --baseline v1.0.2 --runtime compose --installer --derive-encryption-key \
  --cli-matrix --candidate-ref "$candidate_commit"
```

`--runtime compose` and `--runtime helm` isolate a runtime while diagnosing a
failure. `--output /absolute/new-directory` selects the private evidence directory;
it must not exist. The runner prints its `report.json` path. Uncommitted changes
are deliberately excluded from the candidate image. Commit the candidate first,
or supply an existing exact commit available in this checkout.

`--installer` downloads the published installer and Compose release assets and
checks their SHA256SUMS entries. It runs the real installer in a unique project,
then stops its full stack and adopts the same database volume and keys for the
bounded rehearsal. This option requires unused ports 3000, 8000 and 8001; the
published installer binds its normal host ports. Use a disposable local host.
The generated instance environment disables telemetry. Installer output and its
setup link stay private. `--derive-encryption-key` exercises the installer's
default key derivation from SECRET_KEY; omit it to exercise a separate Fernet
key. Neither key changes during the upgrade.

A successful candidate rehearsal leaves `final_rc_gate: pending`. It cannot
claim acceptance of a release that has not been published. The optional GitHub
**Upgrade acceptance** workflow runs the same commands on an ephemeral Linux
runner when manually dispatched. Pull requests run only the harness regression
tests. The workflow uploads only `report.json`.

## What is exercised

1. Install the old release and run its normal initializer. Seed two synthetic
   tenants through that release's models and CRUD layer. The corpus contains
   users with passwords and owner roles, inactive tracker configurations,
   organizations and projects, issues in three states with Unicode/JSON content,
   active and suspended agents, a durable agent credential, active and revoked
   API keys, model credentials, completed flow history, policy snapshots and
   defaults, discovery candidates/evidence and audit rows. The 0.17.x corpus
   includes legacy plaintext credentials for the cutover; 1.0.2 uses its normal
   encrypted credential writers. All values are synthetic. Dormant corpus
   endpoints point at a closed loopback port.
2. Validate baseline password logins, identity resolution, existing API keys,
   agent credential linkage, revoked-key denial, and local resolution of model,
   tracker and webhook secrets. Save a fingerprint of stable IDs, content and
   relationships plus a pre-upgrade login token in process memory.
3. Stop the old API and all other writers before migration. Preserve the database
   volume, JWT secret and encryption key. Take a private database dump. Compose
   runs the candidate release initializer; Helm runs the real `pre-upgrade`
   migration hook and `post-upgrade` initializer. An unreconciled multi-head
   migration graph fails the gate.
4. Repeat the checks under the candidate and authenticate the pre-upgrade token.
   Verify one expected Alembic head. When the legacy API-key column has been
   retired, also require legacy key hashes, encrypted model references and
   encrypted webhook references. Compose repeats initialization and validation
   twice to exercise initializer retries and verify that operator settings persist.
5. Remove only this run's Compose project/volumes and kind cluster, including on
   failure. No global Docker prune, shared cluster deletion, release publication,
   or real provider/model invocation is part of the gate.

Compose derives five services from each version's release file: PostgreSQL,
NATS, initializer, API and gateway. A deterministic mock provider is reachable
only on the internal network. It preserves their commands and health checks, pins
images, supplies synthetic configuration and resource limits, uses an internal
network, and binds the API only to an ephemeral loopback port. Helm renders each
version's real chart with a disposable PostgreSQL PVC and NATS service. A local
post-renderer scales console/background deployments to zero and bounds API
resources. A namespace LimitRange bounds hook containers, since Helm does not
send hooks through post-renderers. Hook commands, environment and image selection
remain those of the released chart.

With `--cli-matrix`, both the published 1.0.2 CLI and the candidate CLI run
inside the API image with disposable homes. All four CLI/server combinations
exercise token login, local discovery, onboarding to a credentialed model,
policy apply/export, gateway response content and usage, and runner WebSocket
connection/reconnection. Password login is independently checked by the corpus.
The candidate/candidate cell also triggers a real flow through the API,
orchestrator and CLI runner. A deterministic local executable substitutes for
the vendor agent CLI and sends its model request through the real gateway to
the mock upstream; its output marker must survive into the persisted result.
This proves dispatch/result plumbing, not a vendor agent's behavior. The simple
HTTP test proxy routes API and gateway traffic; runner WebSockets connect
directly to the API service. Restrictive defaults and discovery-report behavior
are recorded separately because the released server has fewer capabilities.

A missing or failed core matrix operation fails the gate after migration and
restart evidence is collected. Every command records an exit code and elapsed
wall time. The candidate migration/initializer step includes process startup
and default reconciliation; this small synthetic corpus is not production-scale
migration timing.

This is a maintenance-window upgrade test. It does not exercise passkey browser
ceremonies, browser rendering, actual vendor agent binaries, real models,
external secret backends, rolling upgrades with concurrent old writers, or
backup restoration.
Successful credential resolution proves continuity of local synthetic secrets;
it makes no claim that a real provider would accept them. Default kind networking
does not enforce NetworkPolicy; safety comes from explicit local fixtures,
disabled background workers, disabled telemetry and no external credentials.

## Final RC rerun

After a 1.1.0 RC is actually published, resolve its tag from release metadata:

```bash
export PRELOOP_DISABLE_TELEMETRY=true
gh api --paginate 'repos/preloop/preloop/releases?per_page=100' \
  --jq '.[] | select(.draft == false and .prerelease == true and .published_at != null and (.tag_name | test("^v?1[.]1[.]0-rc([.-][0-9]+)?$"))) | .tag_name'
```

Use the exact published `v1.1.0-rc…` tag shown by that command:

```bash
rc_tag='<exact published RC tag>'
python scripts/upgrade_acceptance/runner.py --baseline v1.0.2 \
  --runtime both --installer --derive-encryption-key --cli-matrix --rc "$rc_tag"
```

The runner rejects invented, draft, stable or non-RC release names before starting
any stack. Both runtimes must pass in that run for `final_rc_gate: passed`.
Record the platform and attach the credential-free report to the readiness issue;
a candidate result or a single-runtime result does not satisfy the final gate.
The Compose and chart sources come from the baseline/RC commits, even when the
harness itself is run from a later checkout.

## Evidence and failure recovery

`report.json` is an allowlisted summary suitable for review. Other files are
**private local artifacts**: database backups, generated environment/manifests,
kubeconfig, source exports and diagnostic logs. Do not upload the whole directory.
The report lists failed steps and safe fixture diagnostics; a nonzero exit,
missing runtime, cleanup failure, multiple migration heads, or mismatched source
revision is a failure. No check is silently converted to a pass.

SIGINT triggers cleanup. A host crash or SIGKILL can prevent it. For that case,
read `resource_name` from this run's report and use only its saved configuration:

```bash
artifact_dir='<this run directory>'
resource_name=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["resource_name"])' "$artifact_dir/report.json")
docker compose --project-name "$resource_name" \
  --env-file "$artifact_dir/empty.env" -f "$artifact_dir/compose.json" \
  down --volumes --remove-orphans
kind delete cluster --name "$resource_name" --kubeconfig "$artifact_dir/kubeconfig"
```

Run the applicable cleanup command only for the runtime recorded in that report.
Keep private backups only as long as needed, then delete that specific artifact
directory. Locally built candidate images are uniquely tagged in the report and
can be removed individually with `docker image rm <recorded candidate image>`.

## Recorded candidate rehearsals: 2026-10-22

The [machine-readable evidence](upgrade-acceptance-results.json) records the exact
images, commits, platforms, migration heads, data fingerprints and cleanup results.
These runs used published `v0.17.1` (`b46315580acb3b45cfad7590ca6829d0cef9374d`)
as the baseline, on Linux ARM64 containers. No 1.0.0 RC was published at the time.

| Candidate | Compose | kind/Helm | Result boundary |
| --- | --- | --- | --- |
| Main `726d71a3899af476b1170d9e45a64f7fd544ce26` | Passed, including initializer retry | Passed | Candidate rehearsal; final RC pending |
| Credential PR #1997, `151731bbc672af330bc36991c148a465da769dc5` | Passed, including retry and credential backfill | Passed, including credential backfill | Unmerged candidate; final RC pending |
| Local merge of that credential head and OAuth PR #2000 at `dce0e1e963a26e4999a083f2e98f079130b7b7e9` | Failed at candidate migration | Not run for this combined candidate | Two migration heads must be reconciled before release |

The combined rehearsal used an unpushed detached merge commit
`9a6420db08d05c31b2afdcb88f79c8a0462e1c3d`. It contains a documentation-conflict
resolution and a formatting-only pre-commit change, with no migration edits.
The normal initializer rejected the parallel
`20261110_upstream_mcp_oauth` and `20261111_credential_columns` heads. The baseline
corpus passed before that attempted upgrade; no combined-candidate success is
claimed. This is an integration dependency for the release, not a dependency for
running the harness on current main.

All recorded stacks were removed. Earlier harness development runs found and
fixed a wrong health-check path, Docker containerd multi-platform image export
handling, matching memory/swap limits for kind, and resource defaults for Helm
hooks. The recorded main run and credential Helm run started fresh with those
fixes already applied. The runner has regression coverage for the platform
export and hook limits, and rejects images whose OCI source revision differs
from the selected commit.

To reproduce the main rehearsal exactly:

```bash
export PRELOOP_DISABLE_TELEMETRY=true
git fetch origin main --tags
python scripts/upgrade_acceptance/runner.py --runtime both \
  --candidate-ref 726d71a3899af476b1170d9e45a64f7fd544ce26
```

Use the final-RC procedure above when an RC exists; do not substitute one of
these candidate results for that gate.

## Recorded 1.0.2 candidate rehearsal: 2026-11-25

The [new machine-readable evidence](upgrade-acceptance-1.0.2-results.json)
records successful local Linux ARM64 runs against main snapshot
`0ebc16515e361265800239b34fc32db61d44a563` plus the compatibility fixes:

| Baseline and surface | Candidate commit | Result |
| --- | --- | --- |
| Published 1.0.2 installer / Compose | `3995cd8c0a720673a930891dea36e94b736d8df5` | Upgrade, four CLI/server cells, mocked flow and gateway, two restarts passed |
| Published 1.0.2 / Helm | `9330ab8ae5657cc1109e39463ee44d7424f26f9b` | Chart hooks, content and credentials passed |
| Published 0.17.1 / Compose | `9330ab8ae5657cc1109e39463ee44d7424f26f9b` | Legacy credential cutover and two restarts passed |

The later Compose candidate adds harness assertions and tests; backend and CLI
source are identical across these candidates. The final fixture fingerprint
survived both restarts. Restrictive policy defaults and disabled-tool choices
written by both CLIs also survived both initializer retries. Each run froze its
harness files and recorded their hashes. Owned stacks and clusters were removed.

| CLI | Server | Core commands, gateway and runner reconnect | Capability difference |
| --- | --- | --- | --- |
| 1.0.2 | 1.0.2 | Passed | Restrictive defaults refused; legacy discovery report saved |
| 1.0.2 | Candidate | Passed | Deny defaults and discovery reporting passed |
| Candidate | 1.0.2 | Passed | New discovery evidence could not be saved; explicit guidance and newer-feature refusals verified |
| Candidate | Candidate | Passed | Deny defaults, discovery reporting and mocked flow execution passed |

Core checks cover token login/auth status, local discovery, credentialed
onboarding, policy content readback, gateway response content/usage and real
runner WebSocket handshakes. The candidate CLI refuses review-posting and routing
settings before an older server can silently ignore them. Newer endpoint-based
commands explain missing support; local inventory remains available.

The 1.0.2 migration/initializer took 3.94 seconds on 40 tracked fixture
rows plus matrix tenants and normal initializer data. This includes process
startup and default reconciliation and is **not production-scale timing**.
Version 1.0.2 already stores encrypted credentials: its run proves preservation;
the separate 0.17.1 run exercises plaintext backfill. This remains candidate
evidence; rerun the published RC gate before release acceptance.

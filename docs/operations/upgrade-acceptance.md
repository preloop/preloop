# Rehearsing an upgrade locally

Editions: OSS. This gate exercises the published Docker Compose service definitions
and Helm charts with disposable synthetic data. It never targets an existing
installation or Kubernetes context. It supports [the 1.0 upgrade gate](https://github.com/preloop/preloop/issues/1869).

The runner discovers the highest stable **published** 0.17.x release using the
GitHub releases API. Drafts, prereleases, and unpaired tags are ineligible. It
exports that tag's source, pulls the published backend image, and checks its OCI
source revision against the tag commit. The candidate is either a local image
built from an exact committed tree or the image for an actually published RC.
Every report records commits, image IDs, registry digests, platform, migration
heads, validation results, and cleanup results.

## Prerequisites and candidate rehearsal

Use a disposable Linux Docker host or Docker Desktop with Linux containers,
Python 3.12+, Docker Engine 28+ with Compose v2, Git, authenticated `gh` with public repository
read access, Helm 3, `kubectl`, and kind. The CI recipe pins kind v0.30.0 and
Helm v3.19.0. The runner creates a single local kind node; it never accesses
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
  --runtime both --candidate-ref "$candidate_commit"
```

`--runtime compose` and `--runtime helm` isolate a runtime while diagnosing a
failure. `--output /absolute/new-directory` selects the private evidence directory;
it must not exist. The runner prints its `report.json` path. Uncommitted changes
are deliberately excluded from the candidate image. Commit the candidate first,
or supply an existing exact commit available in this checkout.

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
   API keys, and both legacy and encrypted model credentials. All values are
   synthetic; model endpoints point at a closed loopback port.
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
   to exercise an operator retry.
5. Remove only this run's Compose project/volumes and kind cluster, including on
   failure. No global Docker prune, shared cluster deletion, release publication,
   or provider/model invocation is part of the gate.

Compose derives four services from each version's release file: PostgreSQL,
NATS, initializer and API. It preserves their commands and health checks, pins
images, supplies synthetic configuration and resource limits, uses an internal
network, and binds the API only to an ephemeral loopback port. Helm renders each
version's real chart with a disposable PostgreSQL PVC and NATS service. A local
post-renderer scales console/background deployments to zero and bounds API
resources. A namespace LimitRange bounds hook containers, since Helm does not
send hooks through post-renderers. Hook commands, environment and image selection
remain those of the released chart.

This is a maintenance-window data/authentication upgrade test. It does not
exercise browser rendering, live agent execution, hosted models, external secret
backends, rolling upgrades with concurrent old writers, or backup restoration.
Successful credential resolution proves continuity of local synthetic secrets;
it makes no claim that a real provider would accept them. Default kind networking
does not enforce NetworkPolicy; safety comes from explicit local fixtures,
disabled background workers, disabled telemetry and no external credentials.

## Final RC rerun

After a 1.0.0 RC is actually published, resolve its tag from release metadata:

```bash
export PRELOOP_DISABLE_TELEMETRY=true
gh api --paginate 'repos/preloop/preloop/releases?per_page=100' \
  --jq '.[] | select(.draft == false and .prerelease == true and .published_at != null and (.tag_name | test("^v?1[.]0[.]0-rc([.-][0-9]+)?$"))) | .tag_name'
```

Use the exact published `v1.0.0-rc…` tag shown by that command:

```bash
rc_tag='<exact published RC tag>'
python scripts/upgrade_acceptance/runner.py --runtime both --rc "$rc_tag"
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

# Workspace recovery acceptance

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Issue #386 extends the shipped checkpoint transport rather than adding another
storage path. The following acceptance was exercised on 2026-09-19 using
PostgreSQL, local Docker, and a disposable kind cluster. No model calls or live
application environments were used. Production enablement remains a deployment
step; see [the checkpoint configuration guide](durable-implementation-feedback.md#deployment-prerequisites).

## Required behavior

| Contract | Evidence |
| --- | --- |
| Versioned identity, branch/base/head, digest and age; unpushed/staged/dirty/untracked work; credential/cache exclusion | `test_workspace_recovery_acceptance.py`, `test_flow_artifacts.py`, `test_workspace_snapshot.py`; new base-identity regression covers a never-pushed branch and capture after Git configuration redaction. |
| Authenticated encrypted hosted transport independent of logs; restore before setup | `test_flow_artifacts_db.py` exercises scoped HTTP and encrypted storage. The Docker and Kubernetes integration runs below restored an archive larger than the legacy log limit before cached setup. |
| Periodic recovery, controlled-exit/prepublication barrier and atomic publication | `checkpoint_shell` and the existing publication tests cover final barriers. Integration waits for a periodic upload, writes additional state, forcibly kills Docker's container process or deletes the Kubernetes pod, and restores only the last completed upload. The default interval remains 300 seconds; the test uses 30 seconds. `test_interrupted_or_invalid_upload_does_not_replace_latest` covers interrupted/invalid uploads. |
| Preserve local work when comparing remote heads; explicit missing/unusable recovery | `test_unpublished_workspace_recovery.py` executes the real generated shell against local Git repositories: remote branch absent, behind, equal, divergent, unreachable, and wrong local branch; missing restored repository cannot fall back to cloning. None of these paths overwrite the recovered index or worktree. |
| Retention, byte quotas, zero retention and cleanup leases | `test_workspace_snapshot_cleanup.py`, `test_cleanup_respects_lease_and_reports_expiry`, and Go workspace quota/lease/expiry tests. Metadata continues to report loss of resumability after payload deletion. |
| Private state stays local, owning-runner affinity, visible offline wait and deadline | `test_workspace_recovery_acceptance.py` covers owner/peer lease decisions, offline notices and operator blocking at the deadline, including lookup failures. Go runner tests cover missing workspaces, lease-only directories, expiry tombstones, idle cleanup, active leases and zero retention. These are runner/service tests; no two-host deployment claim is made. |
| Tenant/thread isolation, archive validation and encryption | `test_flow_artifacts.py` and `test_flow_artifacts_db.py` cover scoped retrieval, ciphertext, integrity, unsafe archive members, expansion limits, and expiry. Private storage permissions and operator disk-encryption responsibilities remain documented in the environments guide. |

## Repeat the hosted loss tests

Use a disposable migrated database and the existing environment fixture image.
Pin both the fixture and PostgreSQL image by digest. The script verifies a real
PostgreSQL dependency and a browser interaction, then creates an unpushed Git
commit, forces a failed push, stages a file, edits a tracked file and adds an
untracked file plus more than 2 MiB of incompressible content.

```sh
export PRELOOP_DISABLE_TELEMETRY=true
export DATABASE_URL='<disposable database URL>'
export PYTHONPATH=backend
python scripts/tests/flow_environment_integration.py \
  --image '<fixture image@sha256:...>' \
  --postgres-image '<postgres image@sha256:...>'
```

For Kubernetes, supply a disposable kind kubeconfig, its explicit context, and
the exact loopback API address from that kubeconfig. The guard rejects other
contexts or endpoints. Load the pinned images into the disposable cluster first.
The endpoint must resolve to the test HTTP server from inside the agent pod.

```sh
python scripts/tests/flow_environment_integration.py \
  --image '<fixture image@sha256:...>' \
  --postgres-image '<postgres image@sha256:...>' \
  --kubeconfig '<disposable kubeconfig>' \
  --kube-context kind-preloop-recovery-test \
  --expected-api-server 'https://127.0.0.1:<port>' \
  --loss-mode pod \
  --endpoint 'http://host.docker.internal:25440'
```

Both runs passed: exact commit/index/file recovery, cached setup, nonzero
checkpoint age, and exclusion of writes after the completed checkpoint. The
Kubernetes test deletes the pod with zero grace; the Docker test sends SIGKILL
to the container so child checkpoint processes cannot continue uploading.

The integration test deliberately uses a short interval. It proves the recovery
boundary, not preservation of later writes or production storage capacity. The
64 MiB direct-upload overlay keeps larger archives out of pod logs; deployments
must merge its environment entries, preserve existing encryption keys, and
apply its proxy limits together.

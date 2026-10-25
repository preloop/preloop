# Scanner agent image

Preloop builds an opt-in Codex agent image that carries Trivy and a baked,
digest-pinned advisory database, so a security flow can scan a repository
with no network. The default agent image does not change. The decision and
its measurements are recorded in [Scanner binaries in the agent image](scanner-binaries-decision.md).

## What the image contains

The recipe is `environments/scanner/Dockerfile`. It starts from the default
Codex image pinned by digest and adds:

- Trivy at a pinned version (0.74.0), from the release tarball verified
  against its SHA-256 checksum.
- The Trivy advisory database, downloaded by OCI digest
  (`ghcr.io/aquasecurity/trivy-db@sha256:...`), never by tag, into
  `/opt/trivy-cache`. `TRIVY_CACHE_DIR` points there.
- `/opt/preloop-scanners.json`, which records the Trivy version, the database
  repository with its digest, the database `UpdatedAt` and the build time:

        {"trivy": {"version": "0.74.0",
                   "db_repository": "ghcr.io/aquasecurity/trivy-db@sha256:<digest>",
                   "db_updated_at": "2026-10-11T07:05:14Z",
                   "baked_at": "2026-10-11T12:11:16Z"}}

- `/opt/preloop-environment.json` with `{"version": 1, "harness": "codex"}`,
  which the environment profile setup check requires.

The build fails if the base or the database reference is a tag. Scan
offline with:

    trivy --skip-db-update --skip-java-db-update fs --scanners vuln <path>

The Java advisory database is not baked. The baked database ages from the
day the image was built; `db_updated_at` tells you how old it is.

## Where it is published

The `Publish Scanner Image` workflow pushes
`ghcr.io/preloop/preloop-codex-scanner` on Preloop version tags and writes
the published digest to the run summary. Its daily runs and manual runs with
the default inputs build and smoke the image without pushing.

To recover a failed scanner publication, run the workflow from a corrected
recipe branch with `publish=true` and `release_tag` set to an existing version
tag, such as `v0.17.0`. This publishes only that scanner image tag, never moves
`latest`, and leaves the git tag unchanged. The summary records the recipe
commit separately from the image tag and published digest.

To get a fresher database between releases, build the image yourself with the commands in
`environments/scanner/README.md` and push it to your own registry.

## Register it as an environment profile

Environment profiles live in the operator-owned JSON registry file named by
the `flow_environment_profiles_file` setting (environment variable
`FLOW_ENVIRONMENT_PROFILES_FILE`). Profiles accept only an image pinned by
digest. Add an entry with the published digest:

    {
      "codex-scanner": {
        "version": 1,
        "image": "ghcr.io/preloop/preloop-codex-scanner@sha256:<published digest>",
        "harness": "codex",
        "protocol_version": 1,
        "env": {"TRIVY_CACHE_DIR": "/opt/trivy-cache"},
        "required_executables": ["python3", "git", "bash", "trivy"]
      }
    }

`environments/scanner/profile.example.json` is the same entry with a
placeholder digest. Then select it from a Codex flow with
`"environment_profile": "codex-scanner"` in the agent configuration.
Profiles run on hosted executors only; private runners refuse them.

When a new image is published, replace the digest in the registry file. A
profile never follows a tag.

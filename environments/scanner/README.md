# Opt-in scanner image for Codex

`Dockerfile` here builds a Codex-compatible agent image that adds Trivy at a
pinned version (checksum-verified release tarball) and bakes its advisory
database, fetched by OCI digest rather than by tag. It extends the default
Codex agent image pinned by digest. It is never a default: nothing changes
until an operator registers it as an environment profile.

Build locally:

    environments/scanner/resolve-db-digest.sh   # prints ghcr.io/aquasecurity/trivy-db@sha256:...
    docker build -f environments/scanner/Dockerfile \
      --build-arg TRIVY_DB_REPOSITORY="$(environments/scanner/resolve-db-digest.sh)" \
      -t preloop-codex-scanner:local .
    environments/scanner/run-smoke.sh preloop-codex-scanner:local

The build refuses a base or database reference that is not `name@sha256:...`.
Given the same build arguments the image contents are the same except
`baked_at` in `/opt/preloop-scanners.json`.

Files written into the image:

- `/opt/preloop-scanners.json`: Trivy version, `db_repository` with digest,
  the database `UpdatedAt` (`db_updated_at`) and `baked_at`.
- `/opt/preloop-environment.json`: `{"version": 1, "harness": "codex"}`, the
  environment profile setup protocol marker.
- `/opt/trivy-cache`: the baked database; `TRIVY_CACHE_DIR` points at it.

Scan offline with `trivy --skip-db-update --skip-java-db-update fs --scanners vuln <path>`.
The Java database is not baked.

The offline smoke test scans `/opt/preloop-scanner/fixtures/vulnerable`, a
`requirements.txt` the fetch stage writes with Django 2.2.0 and requests
2.19.0. Those pins stay out of git: OpenSSF Scorecard scans every
`*requirements*.txt` in the tree, including the old urllib3 and idna that
requests 2.19.0 pulls in.

`.github/workflows/publish-scanner-image.yml` publishes the image to
`ghcr.io/preloop/preloop-codex-scanner` on version tags and prints the published
digest. Scheduled runs and manual runs with the default inputs build and smoke
without pushing.
The final stage only uses `COPY --link` to add the prepared files. Set file
permissions in the fetch stage: combining `--link` with `--chmod` makes
BuildKit unpack the large Codex base and can exhaust a hosted runner's disk.
Registering the digest: see `docs/guide/flows/scanner-image.md` and
`profile.example.json`.

To recover a failed scanner publication without moving a release's git tag,
run the workflow on a branch containing the corrected recipe:

    gh workflow run publish-scanner-image.yml --ref <corrected-branch> \
      -f publish=true -f release_tag=v0.17.0

Manual publishing requires an existing version tag and publishes only that
scanner image tag. It never updates `latest` or triggers a full release.
The summary records the recipe commit separately from the image tag and
published digest, since recovery may use a newer recipe than the release.

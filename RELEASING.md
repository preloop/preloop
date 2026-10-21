# Releasing Preloop

Use this checklist before every GitHub release or pre-release.

## Workflow

Follow the [protected-branch release process](docs/releasing.md): prepare a PR,
review and merge it, then tag the exact merged commit with `--finalize`.
Preparation never tags or pushes main. The tag workflow signs and verifies
release assets before publishing. Failed publication must not be worked around
by moving an existing tag.

## Pre-Release Checklist

### Repo and docs

- [ ] `CHANGELOG.md` is accurate and grouped under the target version heading.
- [ ] Any deprecation or removal in this release follows [docs/compatibility.md](docs/compatibility.md) and is in the changelog. Deprecation commits use a subject that starts with `deprecate` (any case), for example `deprecate the legacy flag`. `cliff.toml` groups only those subjects under Deprecated. A commit that mentions deprecation later in the message stays in its own group.
- [ ] `README.md` install steps, feature matrix, and edition boundaries still match the OSS release.
- [ ] `ARCHITECTURE.md` still reflects the shipped product.
- [ ] Contributor-facing repo files are in place: `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue templates.

### Versioning and packaging

- [ ] `./scripts/release.sh <version>` was run for the target version.
- [ ] `VERSION`, Python package version, frontend version, and Helm chart version all match.
- [ ] `server.json`, `CITATION.cff` and `codemeta.json` carry the same version (`scripts/release.py` bumps them; the MCP registry refuses to re-publish a version).
- [ ] Docker, Helm, and GitHub release docs point to the same canonical image names and install paths.
- [ ] GitHub release notes do not advertise artifacts or commands that are not actually published.
- [ ] Release assets include the CLI binaries, Python packages, Helm chart, `docker-compose.release.yaml`, and installer scripts.

### Validation

- [ ] Backend tests pass.
- [ ] Frontend build/tests pass.
- [ ] Any release-blocking smoke tests pass for login, MCP connectivity, tool execution, approvals, and audit visibility.
- [ ] Helm chart lint/package succeeds if Helm artifacts are part of the release.

### Deployment and security

- [ ] Production docs and manifests use the correct auth env vars: `SECRET_KEY` and `ACCESS_TOKEN_EXPIRE_MINUTES`.
- [ ] No default development secrets remain in compose files, Helm values, or example release commands.
- [ ] External dependencies are acceptable for OSS users, especially version checks and push proxy behavior.

### GitHub release

- [ ] Tag uses the expected format: `v<version>`.
- [ ] Release is marked as a pre-release when the version contains a suffix like `-beta.1`.
- [ ] Release body includes accurate changelog notes and working install instructions.
- [ ] Uploaded assets match the release notes.
- [ ] `preloop-v<version>.intoto.jsonl` is attached. Scorecard's Signed-Releases
      check counts that filename on the release, not the Attestations API and
      not `SHA256SUMS`. See [release verification](docs/release-verification.md).
      If `Create Release` fails before the attest step, do not publish the
      assets without the bundle.
- [ ] PyPI prereleases are expected to use PEP 440 normalization, so `0.8.0-beta.1` is published as `0.8.0b1`.

### Catalog listings

- [ ] The Helm chart is at `oci://ghcr.io/preloop/charts/preloop:<version>` and the `artifacthub.io` tag carries `helm/artifacthub-repo.yml` (Artifact Hub reindexes on its own).
- [ ] On a stable tag with `PUSH_TO_DOCKERHUB=true`, the Docker Hub descriptions match `docker/dockerhub/`.
- [ ] Publish the new `server.json` to the official MCP registry: `mcp-publisher login github` then `mcp-publisher publish` from the repo root (an Owner of the `preloop` GitHub org runs it).

### SBOM

The `sbom` job in `.github/workflows/release.yml` emits one CycloneDX 1.6 SBOM
per shipped component and attaches all three to the release. It also runs on
every push to `main`, so there is a current, provenance-attested SBOM between
releases.

- [ ] Three `preloop-sbom-*.cdx.json` assets are present on the release.
- [ ] Their digests appear in `SHA256SUMS`.
- [ ] The job summary's component counts are in the expected range; a sudden
      drop usually means an install step silently failed and the SBOM now
      describes an empty tree.

Regenerate locally (same tool versions as CI):

```bash
./scripts/generate_sbom.sh              # backend, frontend and CLI
./scripts/generate_sbom.sh cli          # one component only
```

The CLI SBOM is only true for one target because Go build constraints select
modules. CI pins `GOOS=linux GOARCH=amd64`, which matches the container images
and the primary release binary.

## First Public OSS Release Gate

Before `v0.8.0-beta.1`, confirm these release-facing areas are resolved:

- Deployment manifests and docs use `SECRET_KEY` and `ACCESS_TOKEN_EXPIRE_MINUTES` consistently.
- Docker Compose, Helm defaults, and GitHub release notes all reference the same image names.
- GitHub releases distinguish between the Python package (`preloop`, including `preloop-sync`) and the standalone CLI binaries.
- `SECURITY.md`, `CODE_OF_CONDUCT.md`, and GitHub contribution templates are present.
- OSS vs Enterprise messaging in public docs is internally consistent.

## Suggested Commands

```bash
./scripts/release.sh 0.17.1 --yes --open-pr
# After review, successful checks, and merging the release PR:
git switch --detach MERGE_COMMIT_SHA
./scripts/release.sh 0.17.1 --finalize --target-commit MERGE_COMMIT_SHA --push-tag
# Resume interrupted publication monitoring:
./scripts/release.sh 0.17.1 --monitor-release
```

Preparation also accepts `--package-chart` and `--generate-changelog-ai`.
See the release process for remote selection and recovery instructions.

# Release SBOM and audit gate

The version-tag Release workflow requires **Release audit** before creating
GitHub release assets or publishing the Python package. Image builders publish
first so the audit can read back exact registry content. An audit failure blocks
the remaining publication; it does not remove already pushed images.

## Image inventory

Each image ships `linux/amd64` and `linux/arm64`. BuildKit generates an SPDX 2.3
inventory from each final filesystem and attaches it to the platform manifest.
The generator is pinned by digest in the workflows.

| Image | Workflow | Version tag |
| --- | --- | --- |
| `ghcr.io/preloop/preloop` | Release | version without `v` |
| `ghcr.io/preloop/console` | Release | version without `v` |
| `ghcr.io/preloop/preloop-codex-scanner` | Publish Scanner Image | full `v…` tag |
| `ghcr.io/preloop/preloop-codex-universal` | Publish Universal Image | full `v…` tag |

When enabled, Docker Hub backend/console mirror inventories are also read back
and compared with GHCR. Moving aliases such as `latest` are not additional image
variants. Runtime plugins publish npm/PyPI packages; their test Dockerfiles are
not release images. GitLab development builds and Enterprise Edition images are
outside this GitHub version-tag inventory.

Scanner builds use native runners per architecture because inventory generation
unpacks the Codex base. Universal's amd64 export retains its build cache until
SBOM export and checks the published image config against the config that passed
its offline smoke. Final scanner/universal indexes merge platform indexes with
their attestations.

The three existing CycloneDX SBOMs remain: installed Python dependencies, the npm
build tree, and the linux/amd64 Go build graph. They complement container
inventories: the console image contains nginx and bundled JavaScript rather than
its original `node_modules` tree.

## Gate and evidence

`scripts/image_sbom.py` requires both image architectures and attached
attestations, checks SPDX identity, a nonempty package inventory and its
relationship graph, then exports per-platform files with content hashes and
index/platform digests. This validates required SPDX fields, not every optional
field. It does not invent supplier or license metadata.

`scripts/release_audit.py collect` waits for the latest **push run for the same
tag and source commit** of CI, CodeQL, Secret Scan, Workflow Audit, Publish
Scanner Image and Publish Universal Image. Main-branch success cannot substitute
for tag evidence. Failed, skipped, cancelled and timed-out runs fail the gate;
missing/pending runs have a bounded wait. CodeQL must also have no open
code-scanning alerts on that tag. API and download failures fail the job.

`validate` requires all four families, eight platform inventories, matching
hashes and commit/tag identities, and all three component SBOMs. It re-measures
component minimum elements using the generator job's existing implementation.
The release job also verifies version tags still resolve to recorded digests and
compares the assets with registry-attached SBOMs, including configured mirrors.
The existing component generator's schema validation remains mandatory.

This gate checks existing security workflows and findings; it does not invoke a
hosted Release Security Audit flow. An inventory alone is not a vulnerability
clearance. No severity exclusions, waivers or VEX suppression are added here.

For a version such as `1.2.3`, published evidence includes:

- `preloop-sbom-{backend,frontend,cli}-1.2.3.cdx.json`
- `preloop-sbom-image-{backend,frontend,scanner,universal}-linux-{amd64,arm64}-1.2.3.spdx.json`
- `preloop-image-{backend,frontend,scanner,universal}-1.2.3.json`: commit, tag,
  image digests, mirror references and SBOM hashes
- `release-audit-runs.json`: exact workflow run IDs, attempts and conclusions
- `release-audit.json`: validated inventory and verification modes

These enter the existing `SHA256SUMS` and signed provenance bundle; see
[release verification](release-verification.md). Asset preparation validates the
downloaded inventory again before hashing it. The release CLI treats missing
image/audit assets as incomplete publication.

## Nonpublishing validation

The **Release validation (nonpublishing)** workflow runs regression tests and a
small real two-platform OCI build on relevant PRs, or by manual dispatch on a
branch. It has read-only permissions, no registry login and no push commands.
Repeat locally from the repository root:

```sh
export PRELOOP_DISABLE_TELEMETRY=true
work_dir="$(mktemp -d)"
python3 -m unittest discover -s backend/tests/scripts -p test_image_sbom.py
python3 -m unittest discover -s backend/tests/scripts -p test_release_audit.py
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false \
  --sbom=generator=docker/buildkit-syft-scanner@sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9 \
  --output "type=oci,dest=$work_dir/image.tar" scripts/tests/sbom_fixture
python3 scripts/image_sbom.py --name backend --oci-archive "$work_dir/image.tar" \
  --tag v0.0.0-fixture --source-sha "$(git rev-parse HEAD)" \
  --output "$work_dir/evidence"
```

The synthetic version creates no git tag. OCI inspection verifies blob hashes
and never extracts archive paths, loads images, or pushes them. This fixture
does not prove production builds or registry publication succeeded.

After downloading a real release's evidence into `release-evidence/`, validation
remains read-only:

```sh
python3 scripts/release_audit.py validate --directory release-evidence \
  --tag "$RELEASE_TAG" --source-sha "$RELEASE_SHA" \
  --verify-registry --require-workflows
```

### First gated release

No published tag carries image SBOM evidence yet, so CI cannot exercise the
remote paths (`collect`, the tag code-scanning query and `--verify-registry`)
before the first tag that runs this workflow. Push a release candidate tag
such as `v1.0.0-rc.1` first and treat its **Release audit** job as the first
remote execution. Expected evidence on that run:

- `collect` logs no `Waiting for` line after the tag workflows complete and
  writes `release-audit-runs.json` with one successful push run per workflow
  for the tag and commit.
- The tag has a code-scanning analysis for `refs/tags/<tag>` at that commit.
  Without one the job fails with `No code-scanning analysis recorded`; it never
  reads a missing analysis as zero findings.
- `release-audit.json` reports `registry_verified: true` and
  `tag_workflows_verified: true`, and the log ends with
  `Release SBOM audit passed`.

Any other outcome blocks publication of that candidate; fix it before the
final tag. To repeat the remote checks for an existing tag without the release
workflow, dispatch **Release validation (nonpublishing)** with the `tag` input.
Its read-only remote audit job downloads that tag's release-run artifacts and
runs `collect` and `validate --verify-registry --require-workflows`.

Offline validation omits the last two flags and reports both verification modes
as false when `--report` is requested, and the command prints that it is not a
passing tag audit.
The final **1.0.0 tag** check in #1870 remains pending until that tag is released
and its actual Release audit job passes.

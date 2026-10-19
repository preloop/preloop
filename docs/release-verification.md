# Signed releases and verification

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Version tags published by `scripts/release.sh` run the GitHub Actions
[Release workflow](https://github.com/preloop/preloop/blob/main/.github/workflows/release.yml). Since v0.15.0, that workflow
uses GitHub OIDC and Sigstore to sign build provenance for the attached release
assets. No developer signing key or additional repository secret is needed.

The provenance covers CLI binaries, Python packages, the Helm chart, installers,
Compose configuration, SBOMs, and `SHA256SUMS`. The attached
`preloop-v<version>.intoto.jsonl` bundle contains the signed statement, its
certificate, and transparency evidence. The workflow verifies every staged
payload and the manifest before creating the GitHub release; a verification
failure stops publication. Checksums cover every payload, excluding the
manifest itself and the bundle generated afterward.

This attestation proves the asset digest and the GitHub workflow identity. It
is not a promise that the source or dependencies are free of vulnerabilities.
It does not cover GitHub's automatically generated source archives or container
images referenced by the Compose file. The Python source distribution attached
by the workflow is covered. Windows Authenticode is separate: it is applied only
when SignPath is configured. See [the code signing policy](code-signing-policy.md).

## Publishing through release.sh

Use the existing interactive release command from the repository root:

```bash
./scripts/release.sh 0.16.0
```

Review the version/changelog changes, run release validation, and use the helper's
commit, tag, and push prompts when ready to publish. Pushing the `v*` tag
starts the release workflow, including automatic attestation. The helper
refuses to push unless `create-release` in this checkout attests
`release-assets/*`, stages `preloop-v<version>.intoto.jsonl`, and passes
release notes through a file. That job creates a draft only after the signed
bundle is on disk, then publishes the draft only after GitHub lists
`preloop-v<version>.intoto.jsonl` among the release assets. A failed upload
stays a draft. After the push the helper watches the tag run. The
`main` run for the same commit does not count: that run skips Create Release
and still finishes green. The helper exits non-zero if that tag run fails or
the published release is missing the bundle or another required asset.
Running the helper only to prepare files does not publish or sign a release.
Its annotated Git tag is not itself a cryptographic signature; the workflow
signs artifact provenance.

Verify a downloaded asset with the commands below. Keep tag creation restricted
to trusted maintainers and protect changes to `.github/workflows/release.yml`:
a signing identity can attest malicious artifacts if its build workflow is
compromised.

## Verify a downloaded asset

Use a current [GitHub CLI](https://cli.github.com/) with `gh attestation verify`.
Select the release tag and its expected commit from a trusted source. The commit
is also included in the verification example in each new release's notes.

```bash
RELEASE_TAG=v0.16.0
RELEASE_COMMIT='<expected-full-commit-sha>'
mkdir -p release-check
cd release-check
gh release download "$RELEASE_TAG" --repo preloop/preloop \
  --pattern preloop-linux-amd64 \
  --pattern "preloop-${RELEASE_TAG}.intoto.jsonl"
gh attestation verify ./preloop-linux-amd64 \
  --bundle "./preloop-${RELEASE_TAG}.intoto.jsonl" \
  --repo preloop/preloop \
  --signer-workflow preloop/preloop/.github/workflows/release.yml \
  --source-ref "refs/tags/${RELEASE_TAG}" \
  --source-digest "$RELEASE_COMMIT" \
  --deny-self-hosted-runners
```

Replace the binary filename to verify another attached asset. A successful exit
validates the artifact digest, signature, trust chain, and expected identity.
Modification of a byte, a bundle from another release, or an unexpected signer
causes verification to fail. An expired network request is a failure to verify,
not a reason to bypass verification. The bundle is local, but the CLI may still
need network access to obtain Sigstore trust roots.

For bulk checksum validation, first verify `SHA256SUMS` using the same command,
then run `sha256sum --check SHA256SUMS` (Linux) or
`shasum -a 256 --check SHA256SUMS` (macOS) after downloading the payloads. Plain
checksums without a trusted signature establish file consistency, not publisher
identity. The installers do not currently enforce these attestations; perform
this verification before executing a downloaded installer or binary.

## OpenSSF Scorecard

The [Signed-Releases check](https://github.com/ossf/scorecard/blob/main/docs/checks.md#signed-releases)
does not read this workflow file or the GitHub Attestations API. Scorecard
v5.5.0 scores up to the last five GitHub releases that have assets. A release
scores 10 when one asset name ends in `.intoto.jsonl`, and 8 for
`.asc`, `.minisig`, `.sig`, `.sign`, or `.sigstore`. `SHA256SUMS` and
Windows Authenticode do not count. The aggregate is the floor of the average.

Signing has been configured since v0.15.0, and v0.15.0 attaches
`preloop-v0.15.0.intoto.jsonl`. The check still scores 2 because four of the
five releases in the window have no such asset: v0.13.0, v0.13.1, and v0.14.0
predate the pipeline, and v0.16.0 was published without the bundle after
`Create Release` failed before the attest step. The workflow on `main` writes
release notes to a file before creating the release, which is what avoids that
failure on the next tag. Each new tag still has to finish attest, verify, and
upload `preloop-v<version>.intoto.jsonl`. Backfilling v0.16.0 is a separate
maintainer action against the bytes already on that release, not something a
source change can mint. Do not treat a workflow file as a signature.

Continue attaching provenance to each real release. Do not manufacture releases
just to move the scoring window. Re-signing a historical binary today can prove
that today's signer endorsed those bytes, but cannot establish how or where the
old binary was built. Do not label that endorsement as original build provenance
or silently replace historical assets. Historical provenance should only be
published when its actual build evidence is available and verifiable.

## Historical artifact endorsements

The manual [Historical release signing workflow](https://github.com/preloop/preloop/blob/main/.github/workflows/historical-release-signing.yml)
is limited to the existing v0.16.0, v0.14.0, v0.13.1 and v0.13.0 releases. It
downloads every original attached asset by API object ID, independently hashes
the bytes, and compares them with the reviewed
`security/historical-release-inventory-2026-10-19.json`. Original asset IDs,
sizes, timestamps, digests and tag commits must remain unchanged. The workflow
does not rebuild packages, move tags, replace assets, publish new releases or
modify release notes.

The new `preloop-<tag>-historical-endorsement.json` names all original assets and
their SHA-256 hashes, including the original checksum manifest when present.
The v0.14.0 and v0.13.1 manifests omit installers and Compose configuration;
v0.13.0 has no manifest. The new endorsement covers these files too. Its signed
statement explicitly describes a **current endorsement of historical bytes**,
not original build provenance or proof that those bytes came from the tag's
source commit. This does not add Windows Authenticode or macOS notarization.

Cosign signs this document using the current main workflow's GitHub OIDC
identity. The detached `preloop-<tag>-historical-endorsement.sigstore` contains the
signature, certificate and transparency evidence. It is not a SLSA attestation.
Scorecard recognizes this detached signature suffix, but that filename check
does not cryptographically verify the signature or the payload hashes.

After the workflow change is reviewed and merged, run its read-only mode first:

```bash
gh workflow run historical-release-signing.yml --repo preloop/preloop \
  --ref main -f publish=false
```

This mode has only `contents: read` and does not request OIDC or create a
signature. Review its inventory and the committed manifest digest before the
signing dispatch. The reviewed inventory for these four releases is:

```text
7abf282bd5805b6dd407eaed2757d77100d72350ca3c3920ebe19d204efcfe1e
```

To sign and attach exactly that inventory:

```bash
gh workflow run historical-release-signing.yml --repo preloop/preloop \
  --ref main -f publish=true \
  -f reviewed_inventory_sha256=7abf282bd5805b6dd407eaed2757d77100d72350ca3c3920ebe19d204efcfe1e
```

The signing job verifies exact issuer, workflow identity and workflow commit
before uploading. It downloads and verifies the two new attachments afterward,
and rechecks that every original asset and tag reference is unchanged. Uploads
never use `--clobber`. If publication is interrupted, the job retains its exact
endorsement documents and bundles in the `historical-release-endorsements`
workflow artifact. Existing or partial endorsement attachments stop a rerun
before signing. Report the partial result; do not delete, replace or silently
re-sign an existing attachment. Recovery requires verifying the retained bytes
against the reviewed inventory and trusted signing commit, comparing any
already attached bytes, and attaching only a missing name.

Verify the published signatures and all original bytes independently, with
cosign v3.1.3 and a trusted copy of the reviewed source:

```bash
SIGNING_COMMIT='<reviewed full main commit used by the signing workflow>'
PRELOOP_DISABLE_TELEMETRY=true python scripts/historical_release_signing.py verify \
  --directory /tmp/preloop-historical-verification \
  --manifest security/historical-release-inventory-2026-10-19.json \
  --manifest-sha256 7abf282bd5805b6dd407eaed2757d77100d72350ca3c3920ebe19d204efcfe1e \
  --signing-sha "$SIGNING_COMMIT"
```

The signing commit must come from the reviewed workflow run or another trusted
record, not from the downloaded endorsement itself. Verification pins
`https://github.com/preloop/preloop/.github/workflows/historical-release-signing.yml@refs/heads/main`
and issuer `https://token.actions.githubusercontent.com`, checks the signing
commit claim, and requires the signed release entry to match the reviewed
inventory. The helper rejects changes, unsafe names, symlinks, incomplete
endorsement pairs and signature failures.

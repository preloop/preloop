# Signed releases and verification

Version tags published by `scripts/release.sh` run the GitHub Actions
[Release workflow](../.github/workflows/release.yml). Since v0.15.0, that workflow
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
commit, tag, push, and monitor prompts when ready to publish. Pushing the `v*` tag
starts the release workflow, including automatic attestation. Running the helper
only to prepare files does not publish or sign a release. Its annotated Git tag
is not itself a cryptographic signature; the workflow signs artifact provenance.

After the workflow succeeds, check that the release has the bundle and verify a
downloaded asset with the commands below. Keep tag creation restricted to trusted
maintainers and protect changes to `.github/workflows/release.yml`: a signing
identity can attest malicious artifacts if its build workflow is compromised.

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
looks for recognized signatures or provenance on recent releases. It assesses a
window of releases, so adding signing does not immediately erase earlier unsigned
entries, and published Scorecard results can lag behind repository changes.
Inspect its current per-release details rather than assuming a particular score.

Continue attaching provenance to each real release. Do not manufacture releases
just to move the scoring window. Re-signing a historical binary today can prove
that today's signer endorsed those bytes, but cannot establish how or where the
old binary was built. Do not label that endorsement as original build provenance
or silently replace historical assets. Historical provenance should only be
published when its actual build evidence is available and verifiable.

# VEX statements

One [OpenVEX](https://openvex.dev) 0.2.0 document lives in this directory.
The release workflow copies every `security/vex/*.openvex.json` into the
SBOM artifact and the GitHub release, next to the CycloneDX files.

The statements live in `scripts/generate_vex.py`, not in the JSON. Edit the
script and rerun. Do not hand-edit a document, because the version counter
and timestamps are derived and OpenVEX consumers use the version to tell a
reissue from an update.

## CLI

There is no CLI OpenVEX document. The CLI used to require
`golang.org/x/crypto` for scrypt, and scanners matched `GO-2026-5932`
(`x/crypto/openpgp`) against the whole module. scrypt now lives in
`cli/internal/scrypt`, using the standard-library PBKDF2, so
`golang.org/x/crypto` is not a dependency. `govulncheck` in the
`cli-vuln-scan` job still fails the build if a vulnerable symbol is called.

## Frontend

`preloop-frontend.openvex.json` asserts that two groups of frontend SBOM
findings do not affect the shipped console.

Regenerate it with:

```bash
python scripts/generate_vex.py --frontend
```

### undici-types

Twenty-four advisories match `undici-types` 7.16.0 because OSV evaluates
git ranges against the undici repository recorded in that package's
`vcs_url`. The package ships only `.d.ts` declarations and contains no
undici runtime. The justification on each statement is
`vulnerable_code_not_present`. The product identifier is
`pkg:npm/undici-types@7.16.0`.

The frontend SBOM stamp also sets `preloop:types_only` to `true` on
`@types/*` and `*-types` packages whose installed files contain no runtime
code. The release security audit may use that property as evidence for
the same conclusion. The property does not itself suppress a finding.

### lodash.camelcase

`CVE-2018-3721` matches `lodash.camelcase` 4.3.0, a frozen per-method
package that never received the lodash 4.17.5 fix. It is required only by
`command-line-args` 5.2.1, a dependency of the dev-only packages
`@web/test-runner` 1.0.0 and `@web/dev-server` 1.0.0. The latest
`command-line-args` (6.0.2) still depends on `lodash.camelcase`, and those
parents are already at their latest release, which still requires
`command-line-args` ^5.1.1, so the package cannot be dropped by a parent
bump. `npm ls --omit=dev` does not list it, and the production frontend
bundle does not contain it. The justification is
`vulnerable_code_not_in_execute_path`. The product identifier is
`pkg:npm/lodash.camelcase@4.3.0`.

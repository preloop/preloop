# VEX statements

Two [OpenVEX](https://openvex.dev) 0.2.0 documents live in this directory.
The release workflow copies every `security/vex/*.openvex.json` into the
SBOM artifact and the GitHub release, next to the CycloneDX files.

The statements live in `scripts/generate_vex.py`, not in the JSON. Edit the
script and rerun. Do not hand-edit a document, because the version counter
and timestamps are derived and OpenVEX consumers use the version to tell a
reissue from an update.

## CLI

`preloop-cli.openvex.json` asserts that the Preloop CLI is `not_affected`
by the four `golang.org/x/crypto` advisories a scanner will otherwise keep
reporting against its SBOM.

Regenerate it with:

```bash
python scripts/generate_vex.py
```

### Why the claim holds

The justification on every statement is `vulnerable_code_not_present`, and
the evidence is `govulncheck`, which classifies a finding at one of three
levels:

| Level | Meaning | Gates CI |
| --- | --- | --- |
| Symbol | The code calls the vulnerable function | Yes |
| Package | The package is imported, the symbol is not called | No |
| Module | The module is required, the package is not imported | No |

All four x/crypto advisories come back at module level. The CLI's only import
from that module is `golang.org/x/crypto/scrypt`, in
`cli/internal/cmd/agents_openclaw.go`. Neither `x/crypto/ssh` nor
`x/crypto/openpgp` is in the import graph.

The `cli-vuln-scan` job in `.github/workflows/ci.yml` reruns that check on
every push and pull request with `-show verbose`, so the levels are in the
log. If an import ever pulls `x/crypto/ssh` into the graph, govulncheck
promotes the finding and the job goes red. That is the signal to rewrite this
document, not to reissue it.

Three of the four are also fixed upstream (0.55.0 and 0.56.0) and the CLI is
past both. `GO-2026-5932` has no fix and never will, which is exactly the
case VEX exists for: the only way to clear it is to say why it does not apply.

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

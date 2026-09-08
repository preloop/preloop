# VEX statements

`preloop-cli.openvex.json` is an [OpenVEX](https://openvex.dev) 0.2.0 document
asserting that the Preloop CLI is `not_affected` by the four
`golang.org/x/crypto` advisories a scanner will otherwise keep reporting
against its SBOM.

Regenerate it with:

```bash
python scripts/generate_vex.py
```

The statements live in `scripts/generate_vex.py`, not in the JSON. Edit the
script and rerun; do not hand-edit the document, because the version counter
and timestamps are derived and OpenVEX consumers use the version to tell a
reissue from an update.

## Why the claim holds

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

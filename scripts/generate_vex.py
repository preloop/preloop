#!/usr/bin/env python3
"""Generate the OpenVEX document for the Preloop CLI.

Why this exists: a scanner reading the CLI's SBOM will keep reporting
golang.org/x/crypto advisories that the shipped binary cannot reach, because
the module is required but the vulnerable packages are never imported. Saying
so once, in a machine-readable form, turns recurring noise into a defensible
statement. That is what VEX is for.

The claims here are not opinion. `govulncheck ./...` in the cli-vuln-scan CI
job classifies every finding at one of three levels, and the x/crypto ones
come back at MODULE level, which means the vulnerable package is not in the
import graph at all. That is precisely the OpenVEX justification
`vulnerable_code_not_present`. If that ever stops being true, govulncheck
promotes the finding to package or symbol level and the CI job goes red,
which is the signal to rewrite this file rather than to reissue it.

Regenerate after any change to the statements below or to cli/go.mod:

    python scripts/generate_vex.py

The document version increments on its own: OpenVEX consumers use it to tell
a reissue from an update, so it must not go backwards.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "security" / "vex" / "preloop-cli.openvex.json"

OPENVEX_CONTEXT = "https://openvex.dev/ns/v0.2.0"
AUTHOR = "Preloop <security@preloop.ai>"
TOOLING = "https://github.com/preloop/preloop/blob/main/scripts/generate_vex.py"

# The one thing every statement here has in common: the CLI's only import
# from golang.org/x/crypto is x/crypto/scrypt, in
# cli/internal/cmd/agents_openclaw.go. Neither x/crypto/ssh nor
# x/crypto/openpgp appears anywhere in the import graph.
SHARED_EVIDENCE = (
    "govulncheck reports this at module level, not package or symbol level, "
    "so the vulnerable package is not in the CLI's import graph. The only "
    "import from golang.org/x/crypto is golang.org/x/crypto/scrypt, in "
    "cli/internal/cmd/agents_openclaw.go. Re-verified by the cli-vuln-scan "
    "job in .github/workflows/ci.yml on every push and pull request."
)

# (advisory id, aliases, one-line title, extra impact note)
ADVISORIES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "GO-2026-5932",
        (),
        "golang.org/x/crypto/openpgp is unmaintained and unsafe by design",
        "There is no fixed version of this advisory and there will not be "
        "one, so a version bump cannot clear it. The package is not imported.",
    ),
    (
        "GO-2026-6303",
        ("CVE-2026-56854",),
        "Source-address critical option not enforced for non-public-key auth "
        "callbacks in golang.org/x/crypto/ssh",
        "Additionally fixed upstream in golang.org/x/crypto v0.55.0, which "
        "the CLI is past. The package is not imported either way.",
    ),
    (
        "GO-2026-6354",
        ("CVE-2026-78662",),
        "Prevent DoS on deadlocked undecided channel in golang.org/x/crypto/ssh",
        "Additionally fixed upstream in golang.org/x/crypto v0.56.0, which "
        "the CLI is on. The package is not imported either way.",
    ),
    (
        "GO-2026-6355",
        ("CVE-2026-56855",),
        "Prevent DoS on deadlocked established channel in golang.org/x/crypto/ssh",
        "Additionally fixed upstream in golang.org/x/crypto v0.56.0, which "
        "the CLI is on. The package is not imported either way.",
    ),
)


def read_product_version() -> str:
    return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def read_module_version(module: str) -> str:
    """Read a module's version out of cli/go.mod so this cannot drift."""
    go_mod = (REPO_ROOT / "cli" / "go.mod").read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(module)}\s+(v\S+)", go_mod, re.MULTILINE)
    if not match:
        raise SystemExit(
            f"{module} not found in cli/go.mod; update scripts/generate_vex.py"
        )
    return match.group(1)


def previous_version(path: Path) -> int:
    """Return the version of an existing document, or 0 if there is none."""
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 0))
    except (ValueError, json.JSONDecodeError):
        return 0


def build_document(timestamp: str, version: int) -> dict[str, Any]:
    product_version = read_product_version()
    crypto_version = read_module_version("golang.org/x/crypto")

    product_purl = f"pkg:golang/github.com/preloop/preloop/cli@v{product_version}"
    subcomponent_purl = f"pkg:golang/golang.org/x/crypto@{crypto_version}"

    statements = []
    for advisory, aliases, title, note in ADVISORIES:
        vulnerability: dict[str, Any] = {
            "@id": f"https://pkg.go.dev/vuln/{advisory}",
            "name": advisory,
            "description": title,
        }
        if aliases:
            vulnerability["aliases"] = list(aliases)
        statements.append(
            {
                "vulnerability": vulnerability,
                "timestamp": timestamp,
                "products": [
                    {
                        "@id": product_purl,
                        "identifiers": {"purl": product_purl},
                        "subcomponents": [
                            {
                                "@id": subcomponent_purl,
                                "identifiers": {"purl": subcomponent_purl},
                            }
                        ],
                    }
                ],
                "status": "not_affected",
                "justification": "vulnerable_code_not_present",
                "impact_statement": f"{note} {SHARED_EVIDENCE}",
            }
        )

    return {
        "@context": OPENVEX_CONTEXT,
        "@id": f"https://preloop.ai/vex/preloop-cli-{product_version}-{version}",
        "author": AUTHOR,
        "timestamp": timestamp,
        "last_updated": timestamp,
        "version": version,
        "tooling": TOOLING,
        "statements": statements,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="where to write the document (default: %(default)s)",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="RFC 3339 timestamp to stamp, for a reproducible regeneration",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print the document instead of writing it",
    )
    args = parser.parse_args(argv)

    timestamp = args.timestamp or dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    document = build_document(timestamp, previous_version(args.output) + 1)
    rendered = json.dumps(document, indent=2) + "\n"

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {args.output} (version {document['version']}, "
        f"{len(document['statements'])} statements)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

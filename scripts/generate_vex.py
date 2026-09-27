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

The frontend document is separate. It states that ``undici-types`` ships
only TypeScript declarations, so undici runtime advisories matched through
the package repository URL do not apply, and that ``lodash.camelcase`` is
not on the shipped frontend execute path:

    python scripts/generate_vex.py --frontend

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
FRONTEND_OUTPUT = REPO_ROOT / "security" / "vex" / "preloop-frontend.openvex.json"

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


# Versions are pinned here and checked against frontend/package-lock.json.
# A lockfile bump that moves either package must update this file, because
# the statements are about these exact versions.
UNDICI_TYPES_PACKAGE = "undici-types"
UNDICI_TYPES_VERSION = "7.16.0"
LODASH_CAMELCASE_PACKAGE = "lodash.camelcase"
LODASH_CAMELCASE_VERSION = "4.3.0"

# OSV matched these through the undici git repository named by undici-types'
# vcs_url. The package itself is declarations only.
UNDICI_ADVISORIES: tuple[str, ...] = (
    "CVE-2026-1525",
    "CVE-2026-1526",
    "CVE-2026-1527",
    "CVE-2026-1528",
    "CVE-2026-2229",
    "CVE-2026-6733",
    "CVE-2026-9678",
    "CVE-2026-9679",
    "CVE-2026-11525",
    "CVE-2026-12151",
    "CVE-2026-13697",
    "CVE-2026-14643",
    "CVE-2026-15157",
    "CVE-2026-16728",
    "CVE-2026-16729",
    "CVE-2026-18149",
    "CVE-2026-18540",
    "CVE-2026-19534",
    "CVE-2026-22036",
    "CVE-2026-84890",
    "CVE-2026-84933",
    "CVE-2026-84947",
    "CVE-2026-85008",
    "CVE-2026-85014",
)

UNDICI_IMPACT = (
    "undici-types ships only TypeScript declaration files (.d.ts) and "
    "contains no undici runtime. These advisories describe defects in the "
    "undici runtime. The package repository field points at the undici git "
    "repository, so a scanner that matches OSV git ranges through vcs_url "
    "attributes those runtime advisories to a declarations package that "
    "does not contain the vulnerable code."
)

LODASH_IMPACT = (
    "CVE-2018-3721 is prototype pollution fixed in lodash 4.17.5. "
    "lodash.camelcase 4.3.0 is a frozen per-method package that never "
    "received that fix. It is required only by command-line-args 5.2.1, "
    "a dependency of the dev-only packages @web/test-runner 1.0.0 and "
    "@web/dev-server 1.0.0. command-line-args 6.0.2, the latest release, "
    "still depends on lodash.camelcase, and those parents are already at "
    "their latest 1.0.0 release, which still requires command-line-args "
    "^5.1.1. npm ls --omit=dev does not list the package, and the "
    "production frontend bundle does not contain it. The vulnerable code "
    "is not on the shipped execute path."
)


def read_npm_package_version(package: str) -> str:
    """Read one package version from ``frontend/package-lock.json``."""
    lock_path = REPO_ROOT / "frontend" / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock.get("packages")
    entry = (
        packages.get(f"node_modules/{package}") if isinstance(packages, dict) else None
    )
    version = entry.get("version") if isinstance(entry, dict) else None
    if not isinstance(version, str):
        raise SystemExit(
            f"{package} not found in frontend/package-lock.json; "
            "update scripts/generate_vex.py"
        )
    return version


def require_npm_version(package: str, expected: str) -> str:
    """Return ``expected`` or exit when the lockfile has moved on."""
    found = read_npm_package_version(package)
    if found != expected:
        raise SystemExit(
            f"{package} is {found} in frontend/package-lock.json, "
            f"expected {expected}; update scripts/generate_vex.py"
        )
    return found


def npm_product(name: str, version: str) -> dict[str, Any]:
    """One OpenVEX product whose identifier is the npm purl."""
    purl = f"pkg:npm/{name}@{version}"
    return {"@id": purl, "identifiers": {"purl": purl}}


def _statement(
    *,
    advisory: str,
    description: str,
    timestamp: str,
    product: dict[str, Any],
    justification: str,
    impact_statement: str,
) -> dict[str, Any]:
    return {
        "vulnerability": {
            "@id": f"https://nvd.nist.gov/vuln/detail/{advisory}",
            "name": advisory,
            "description": description,
        },
        "timestamp": timestamp,
        "products": [product],
        "status": "not_affected",
        "justification": justification,
        "impact_statement": impact_statement,
    }


def build_frontend_document(
    timestamp: str,
    version: int,
    product_version: str | None = None,
) -> dict[str, Any]:
    """Build the frontend OpenVEX document.

    Args:
        timestamp: RFC 3339 timestamp stamped on the document and statements.
        version: OpenVEX document version. Must not go backwards.
        product_version: Product version embedded in the document id.
            Defaults to the ``VERSION`` file. A parity test passes the
            version already stamped on the checked-in document so a later
            bump of ``VERSION`` does not fail CI before regeneration.

    Returns:
        An OpenVEX 0.2.0 document. ``undici-types`` statements use
        ``vulnerable_code_not_present``. The ``lodash.camelcase`` statement
        uses ``vulnerable_code_not_in_execute_path``.
    """
    if product_version is None:
        product_version = read_product_version()
    undici_version = require_npm_version(UNDICI_TYPES_PACKAGE, UNDICI_TYPES_VERSION)
    lodash_version = require_npm_version(
        LODASH_CAMELCASE_PACKAGE, LODASH_CAMELCASE_VERSION
    )
    undici = npm_product(UNDICI_TYPES_PACKAGE, undici_version)
    lodash = npm_product(LODASH_CAMELCASE_PACKAGE, lodash_version)

    statements = [
        _statement(
            advisory=advisory,
            description=(
                "undici runtime advisory matched through the undici-types "
                "repository URL"
            ),
            timestamp=timestamp,
            product=undici,
            justification="vulnerable_code_not_present",
            impact_statement=UNDICI_IMPACT,
        )
        for advisory in UNDICI_ADVISORIES
    ]
    statements.append(
        _statement(
            advisory="CVE-2018-3721",
            description="Prototype pollution in lodash before 4.17.5",
            timestamp=timestamp,
            product=lodash,
            justification="vulnerable_code_not_in_execute_path",
            impact_statement=LODASH_IMPACT,
        )
    )
    return {
        "@context": OPENVEX_CONTEXT,
        "@id": (f"https://preloop.ai/vex/preloop-frontend-{product_version}-{version}"),
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
    parser.add_argument(
        "--frontend",
        action="store_true",
        help="write the frontend document instead of the CLI document",
    )
    args = parser.parse_args(argv)

    timestamp = args.timestamp or dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    output = args.output
    if args.frontend and output == DEFAULT_OUTPUT:
        output = FRONTEND_OUTPUT
    builder = build_frontend_document if args.frontend else build_document
    document = builder(timestamp, previous_version(output) + 1)
    rendered = json.dumps(document, indent=2) + "\n"

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {output} (version {document['version']}, "
        f"{len(document['statements'])} statements)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Generate the OpenVEX document for the frontend SBOM.

The CLI document is gone. The CLI used to require ``golang.org/x/crypto``
only for scrypt, and scanners kept matching ``GO-2026-5932``
(``x/crypto/openpgp``) against that module even though the package was
never imported. scrypt now lives in ``cli/internal/scrypt`` and the module
is not required, so there is nothing left for those statements to cover.

The frontend document states that ``undici-types`` ships only TypeScript
declarations, so undici runtime advisories matched through the package
repository URL do not apply, and that ``lodash.camelcase`` is not on the
shipped frontend execute path:

    python scripts/generate_vex.py --frontend

The document version increments on its own: OpenVEX consumers use it to tell
a reissue from an update, so it must not go backwards.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_OUTPUT = REPO_ROOT / "security" / "vex" / "preloop-frontend.openvex.json"

OPENVEX_CONTEXT = "https://openvex.dev/ns/v0.2.0"
AUTHOR = "Preloop <security@preloop.ai>"
TOOLING = "https://github.com/preloop/preloop/blob/main/scripts/generate_vex.py"


def read_product_version() -> str:
    return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def previous_version(path: Path) -> int:
    """Return the version of an existing document, or 0 if there is none."""
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 0))
    except (ValueError, json.JSONDecodeError):
        return 0


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
        default=FRONTEND_OUTPUT,
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
        help="accepted for compatibility; the only document is the frontend one",
    )
    args = parser.parse_args(argv)
    # --frontend remains so the documented command keeps working.
    _ = args.frontend

    timestamp = args.timestamp or dt.datetime.now(dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")

    output = args.output
    document = build_frontend_document(timestamp, previous_version(output) + 1)
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

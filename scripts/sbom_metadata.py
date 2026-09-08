#!/usr/bin/env python3
"""Stamp manufacturer metadata onto CycloneDX SBOMs and measure their quality.

Two jobs, both driven by the NTIA minimum elements:

1. Fill the fields the generators leave empty. syft and the CycloneDX
   generators emit ``metadata.authors: null`` and no ``metadata.supplier``,
   which is an NTIA miss on every SBOM this repo would otherwise publish.
   The values come from ``pyproject.toml`` so there is one source of truth.

2. Print a quality table. The percentages are the same ones an SBOM
   verification flow scores, so a regression is visible in the build log
   instead of in a customer's audit.

Usage:
    python scripts/sbom_metadata.py sbom/*.cdx.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

SUPPLIER = {
    "name": "Preloop",
    "url": ["https://preloop.ai"],
    "contact": [{"name": "Preloop Security", "email": "security@preloop.ai"}],
}


def read_authors(pyproject: Path) -> list[dict[str, str]]:
    """Return CycloneDX organizationalContact entries from PEP 621 authors."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    authors = data.get("project", {}).get("authors", [])
    contacts: list[dict[str, str]] = []
    for author in authors:
        contact = {key: author[key] for key in ("name", "email") if author.get(key)}
        if contact:
            contacts.append(contact)
    return contacts


def stamp(document: dict[str, Any], authors: list[dict[str, str]]) -> None:
    metadata = document.setdefault("metadata", {})
    if not metadata.get("authors"):
        metadata["authors"] = authors
    if not metadata.get("supplier"):
        metadata["supplier"] = SUPPLIER
    if not metadata.get("manufacturer"):
        metadata["manufacturer"] = SUPPLIER
    component = metadata.get("component")
    if isinstance(component, dict):
        if not component.get("supplier"):
            component["supplier"] = SUPPLIER
        if not component.get("authors"):
            component["authors"] = authors


def has_licence(component: dict[str, Any]) -> bool:
    # cyclonedx-gomod reports detected licences under `evidence.licenses`
    # rather than `licenses`, because detection is inference, not a
    # declaration. Both count as "the consumer can see a licence".
    if component.get("licenses"):
        return True
    return bool((component.get("evidence") or {}).get("licenses"))


def has_supplier(component: dict[str, Any]) -> bool:
    # A component counts as attributed if any of the three fields a consumer
    # would read for provenance is populated. `author` is deprecated in 1.6
    # but still what most Python and npm generators emit.
    return bool(
        component.get("supplier")
        or component.get("publisher")
        or component.get("author")
        or component.get("authors")
    )


def measure(document: dict[str, Any]) -> dict[str, Any]:
    components = document.get("components", [])
    total = len(components)

    def pct(count: int) -> str:
        return f"{(100.0 * count / total):.1f}%" if total else "n/a"

    versions = sum(1 for c in components if c.get("version"))
    purls = sum(1 for c in components if c.get("purl"))
    licences = sum(1 for c in components if has_licence(c))
    suppliers = sum(1 for c in components if has_supplier(c))
    return {
        "components": total,
        "version": pct(versions),
        "purl": pct(purls),
        "licence": pct(licences),
        "supplier": pct(suppliers),
        "dependencies": len(document.get("dependencies", [])),
        "authors": len(document.get("metadata", {}).get("authors") or []),
    }


def build_validator() -> Any:
    """Return a CycloneDX 1.6 strict validator, or exit with a clear message."""
    try:
        from cyclonedx.schema import SchemaVersion
        from cyclonedx.validation.json import JsonStrictValidator
    except ImportError:  # pragma: no cover - depends on the caller's interpreter
        print(
            "--validate needs cyclonedx-python-lib; run this with the interpreter "
            "from a venv built off .github/requirements/sbom.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return JsonStrictValidator(SchemaVersion.V1_6)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sboms", nargs="+", type=Path, help="CycloneDX JSON files")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=REPO_ROOT / "pyproject.toml",
        help="PEP 621 file the author list is read from",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; fail if any file is missing supplier or authors",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help=(
            "additionally validate each file against the CycloneDX 1.6 strict "
            "schema (needs cyclonedx-python-lib, see .github/requirements/sbom.txt)"
        ),
    )
    args = parser.parse_args(argv)

    authors = read_authors(args.pyproject)
    if not authors:
        print(f"no [project].authors in {args.pyproject}", file=sys.stderr)
        return 1

    header = f"{'sbom':<44}{'comps':>7}{'ver':>8}{'purl':>8}{'lic':>8}{'suppl':>8}{'deps':>7}"
    print(header)
    print("-" * len(header))

    validator = build_validator() if args.validate else None

    failures = 0
    for path in args.sboms:
        document = json.loads(path.read_text(encoding="utf-8"))
        if args.check:
            metadata = document.get("metadata", {})
            if not metadata.get("authors") or not metadata.get("supplier"):
                print(
                    f"{path}: missing metadata.authors or metadata.supplier",
                    file=sys.stderr,
                )
                failures += 1
        else:
            stamp(document, authors)
            path.write_text(
                json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8"
            )

        if validator is not None:
            error = validator.validate_str(path.read_text(encoding="utf-8"))
            if error is not None:
                print(f"{path}: not valid CycloneDX 1.6: {error}", file=sys.stderr)
                failures += 1

        stats = measure(document)
        print(
            f"{path.name:<44}{stats['components']:>7}{stats['version']:>8}"
            f"{stats['purl']:>8}{stats['licence']:>8}{stats['supplier']:>8}"
            f"{stats['dependencies']:>7}"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

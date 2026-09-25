#!/usr/bin/env python3
"""Stamp manufacturer metadata onto CycloneDX SBOMs and measure their quality.

Two jobs, both driven by the NTIA minimum elements:

1. Fill the fields the generators leave empty. syft and the CycloneDX
   generators emit ``metadata.authors: null`` and no ``metadata.supplier``,
   which is an NTIA miss on every SBOM this repo would otherwise publish.
   The document supplier, manufacturer and root component come from
   ``pyproject.toml``. Every other component gets its own supplier from a
   local, offline source (installed ``METADATA``, ``package.json``, or the
   Go module path). ``author`` stays author. The property
   ``preloop:supplier_source`` records which derivation was used.

2. Print a quality table. The supplier column counts ``supplier.name``
   only, the same rule as ``python -m preloop.cra measure``. Author and
   publisher do not count.

Usage:
    python scripts/sbom_metadata.py sbom/*.cdx.json
    python scripts/sbom_metadata.py --python-root /path/to/venv \\
        --npm-root frontend/node_modules sbom/*.cdx.json
"""

from __future__ import annotations

import argparse
import email
import json
import re
import sys
import tomllib
from email import policy
from pathlib import Path
from typing import Any
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent

SUPPLIER = {
    "name": "Preloop",
    "url": ["https://preloop.ai"],
    "contact": [{"name": "Preloop Security", "email": "security@preloop.ai"}],
}

SUPPLIER_SOURCE_PROPERTY = "preloop:supplier_source"
SOURCE_AUTHOR = "package_metadata_author"
SOURCE_MAINTAINER = "package_metadata_maintainer"
SOURCE_NPM_SCOPE = "npm_scope"
SOURCE_MODULE_PATH = "module_path"
SOURCE_UNRESOLVED = "unresolved"

_PERSON_RE = re.compile(
    r"^\s*(?P<name>[^<(]*?)\s*"
    r"(?:<(?P<email>[^>]+)>)?\s*"
    r"(?:\((?P<url>[^)]+)\))?\s*$"
)


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
    """Fill document-level supplier, manufacturer and authors when empty."""
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
    """True when CycloneDX ``supplier.name`` is set.

    Author, authors and publisher are not a supplier. The platform's
    minimum-elements measurement uses the same rule.
    """
    supplier = component.get("supplier")
    if not isinstance(supplier, dict):
        return False
    name = supplier.get("name")
    return isinstance(name, str) and bool(name.strip())


def measure(document: dict[str, Any]) -> dict[str, Any]:
    """Return quality percentages for the components array."""
    components = list(iter_components(document))
    total = len(components)

    def pct(count: int) -> str:
        return f"{(100.0 * count / total):.1f}%" if total else "n/a"

    versions = sum(1 for c in components if c.get("version"))
    purls = sum(1 for c in components if c.get("purl"))
    licences = sum(1 for c in components if has_licence(c))
    suppliers = sum(1 for c in components if has_supplier(c))
    unresolved = sum(1 for c in components if supplier_source(c) == SOURCE_UNRESOLVED)
    return {
        "components": total,
        "version": pct(versions),
        "purl": pct(purls),
        "licence": pct(licences),
        "supplier": pct(suppliers),
        "unresolved": unresolved,
        "dependencies": len(document.get("dependencies", [])),
        "authors": len(document.get("metadata", {}).get("authors") or []),
    }


def normalize_name(name: str) -> str:
    """Normalize a distribution name the way pip compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_person(value: str) -> dict[str, str] | None:
    """Parse ``Name <email> (url)`` into name, email and url parts."""
    text = value.strip()
    if not text:
        return None
    match = _PERSON_RE.match(text)
    if match is None:
        return {"name": text}
    name = (match.group("name") or "").strip().strip(",").strip()
    email_address = (match.group("email") or "").strip()
    url = (match.group("url") or "").strip()
    if not name and email_address:
        name = email_address
    if not name:
        return None
    person: dict[str, str] = {"name": name}
    if email_address:
        person["email"] = email_address
    if url:
        person["url"] = url
    return person


def _header_people(message: email.message.Message, field: str) -> list[dict[str, str]]:
    people: list[dict[str, str]] = []
    for raw in message.get_all(field, []):
        if not isinstance(raw, str):
            continue
        for part in raw.split(","):
            person = parse_person(part)
            if person is not None:
                people.append(person)
    return people


def people_from_metadata(
    text: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return ``(authors, maintainers)`` from a ``METADATA`` or ``PKG-INFO`` body."""
    message = email.message_from_string(text, policy=policy.compat32)
    authors = _header_people(message, "Author")
    authors.extend(_header_people(message, "Author-email"))
    maintainers = _header_people(message, "Maintainer")
    maintainers.extend(_header_people(message, "Maintainer-email"))
    return authors, maintainers


def people_from_pyproject(
    text: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return ``(authors, maintainers)`` from a wheel's ``pyproject.toml``."""
    data = tomllib.loads(text)
    project = data.get("project", {})

    def contacts(key: str) -> list[dict[str, str]]:
        found: list[dict[str, str]] = []
        for entry in project.get(key, []) or []:
            if isinstance(entry, str):
                person = parse_person(entry)
            elif isinstance(entry, dict):
                person = {
                    field: entry[field]
                    for field in ("name", "email")
                    if isinstance(entry.get(field), str) and entry[field].strip()
                }
                if not person.get("name") and person.get("email"):
                    person["name"] = person["email"]
                person = person or None
            else:
                person = None
            if person and person.get("name"):
                found.append(person)
        return found

    return contacts("authors"), contacts("maintainers")


def supplier_from_people(
    authors: list[dict[str, str]], maintainers: list[dict[str, str]]
) -> tuple[dict[str, Any], str] | None:
    """Pick one supplier. Authors win over maintainers. Never invent a name."""
    if authors:
        return _supplier_dict(authors[0]), SOURCE_AUTHOR
    if maintainers:
        return _supplier_dict(maintainers[0]), SOURCE_MAINTAINER
    return None


def _supplier_dict(person: dict[str, str]) -> dict[str, Any]:
    supplier: dict[str, Any] = {"name": person["name"]}
    if person.get("url"):
        supplier["url"] = [person["url"]]
    contact: dict[str, str] = {}
    if person.get("name") and person["name"] != person.get("email"):
        contact["name"] = person["name"]
    if person.get("email"):
        contact["email"] = person["email"]
    if contact:
        supplier["contact"] = [contact]
    return supplier


def parse_purl(purl: str) -> tuple[str, str] | None:
    """Return ``(type, name-path)`` from a Package URL, without version."""
    if not purl.startswith("pkg:"):
        return None
    body = purl[4:]
    body = body.split("?", 1)[0].split("#", 1)[0]
    ecosystem, _, remainder = body.partition("/")
    if not ecosystem or not remainder:
        return None
    name = unquote(remainder.rsplit("@", 1)[0])
    return ecosystem.lower(), name


def iter_components(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every component except the metadata root, including nested ones."""
    metadata = document.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    root_ref = root.get("bom-ref") if isinstance(root, dict) else None
    seen: set[int] = set()
    found: list[dict[str, Any]] = []

    def walk(node: Any, *, is_root: bool) -> None:
        if not isinstance(node, dict):
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        if not is_root:
            found.append(node)
        children = node.get("components")
        if isinstance(children, list):
            for child in children:
                walk(child, is_root=False)

    components = document.get("components")
    if isinstance(components, list):
        for node in components:
            is_root = (
                isinstance(node, dict)
                and isinstance(root_ref, str)
                and node.get("bom-ref") == root_ref
            )
            walk(node, is_root=is_root)
    return found


def supplier_source(component: dict[str, Any]) -> str | None:
    """Return the ``preloop:supplier_source`` value, if this script set one."""
    properties = component.get("properties")
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == SUPPLIER_SOURCE_PROPERTY:
            value = prop.get("value")
            if isinstance(value, str):
                return value
    return None


def set_supplier_source(component: dict[str, Any], source: str) -> None:
    """Record how ``supplier`` was chosen. Replace a previous stamp of ours."""
    properties = component.get("properties")
    if not isinstance(properties, list):
        properties = []
        component["properties"] = properties
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == SUPPLIER_SOURCE_PROPERTY:
            prop["value"] = source
            return
    properties.append({"name": SUPPLIER_SOURCE_PROPERTY, "value": source})


class MetadataIndex:
    """Offline lookup of installed Python and npm package metadata."""

    def __init__(self, python_roots: list[Path], npm_roots: list[Path]) -> None:
        self._python: dict[str, list[Path]] = {}
        self._npm: dict[str, Path] = {}
        for root in python_roots:
            self._index_python(root)
        for root in npm_roots:
            self._index_npm(root)

    def _index_python(self, root: Path) -> None:
        if not root.is_dir():
            return
        for metadata in root.rglob("METADATA"):
            if not metadata.parent.name.endswith(".dist-info"):
                continue
            try:
                message = email.message_from_string(
                    metadata.read_text(encoding="utf-8", errors="replace"),
                    policy=policy.compat32,
                )
            except OSError:
                continue
            name = message.get("Name")
            if isinstance(name, str) and name.strip():
                self._python.setdefault(normalize_name(name), []).append(metadata)

    def _index_npm(self, root: Path) -> None:
        if not root.is_dir():
            return
        for manifest in root.rglob("package.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            name = data.get("name")
            if not isinstance(name, str) or not name:
                continue
            current = self._npm.get(name)
            if current is None or len(manifest.parts) < len(current.parts):
                self._npm[name] = manifest

    def python_metadata(self, name: str) -> list[Path]:
        return list(self._python.get(normalize_name(name), []))

    def npm_manifest(self, name: str) -> Path | None:
        direct = self._npm.get(name)
        if direct is not None:
            return direct
        return None


def _python_supplier(paths: list[Path]) -> tuple[dict[str, Any], str] | None:
    for metadata in paths:
        try:
            authors, maintainers = people_from_metadata(
                metadata.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
        chosen = supplier_from_people(authors, maintainers)
        if chosen is not None:
            return chosen
        pyproject = metadata.parent / "pyproject.toml"
        if pyproject.is_file():
            try:
                authors, maintainers = people_from_pyproject(
                    pyproject.read_text(encoding="utf-8")
                )
            except (OSError, tomllib.TOMLDecodeError):
                continue
            chosen = supplier_from_people(authors, maintainers)
            if chosen is not None:
                return chosen
    return None


def _npm_person(value: Any) -> dict[str, str] | None:
    if isinstance(value, str):
        return parse_person(value)
    if isinstance(value, dict):
        name = value.get("name")
        email_address = value.get("email")
        url = value.get("url")
        person: dict[str, str] = {}
        if isinstance(name, str) and name.strip():
            person["name"] = name.strip()
        if isinstance(email_address, str) and email_address.strip():
            person["email"] = email_address.strip()
        if isinstance(url, str) and url.strip():
            person["url"] = url.strip()
        if not person.get("name") and person.get("email"):
            person["name"] = person["email"]
        if person.get("name"):
            return person
    return None


def _npm_people(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        people = [_npm_person(item) for item in value]
        return [person for person in people if person is not None]
    person = _npm_person(value)
    return [person] if person is not None else []


def _npm_supplier(manifest: Path) -> tuple[dict[str, Any], str] | None:
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    authors = _npm_people(data.get("author"))
    authors.extend(_npm_people(data.get("contributors")))
    maintainers = _npm_people(data.get("maintainers"))
    return supplier_from_people(authors, maintainers)


def _npm_scope_supplier(name: str) -> tuple[dict[str, Any], str] | None:
    if not name.startswith("@") or "/" not in name:
        return None
    scope = name.split("/", 1)[0]
    if scope == "@":
        return None
    return {"name": scope}, SOURCE_NPM_SCOPE


def supplier_from_path(module_path: str) -> tuple[dict[str, Any], str] | None:
    """Apply the module-path rule to a Go module or a repository path.

    ``golang.org/x`` and ``std`` are The Go Authors. ``github.com/<org>``
    uses the org. Any other host uses the host plus the first path segment.
    A shorthand ``org/repo`` is treated as GitHub.
    """
    path = module_path.strip().strip("/")
    path = re.sub(r"^(git\+|git:|https?://)", "", path)
    path = path.split("#", 1)[0].split("?", 1)[0]
    path = path.removesuffix(".git")
    if path.startswith("github:"):
        path = "github.com/" + path[len("github:") :]
    if not path or path == "std" or path.startswith("std/"):
        return {"name": "The Go Authors"}, SOURCE_MODULE_PATH
    if path == "golang.org/x" or path.startswith("golang.org/x/"):
        return {"name": "The Go Authors"}, SOURCE_MODULE_PATH
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and "." not in parts[0]:
        parts = ["github.com", *parts]
    if len(parts) >= 2 and parts[0] == "github.com" and parts[1]:
        return {"name": parts[1]}, SOURCE_MODULE_PATH
    if len(parts) >= 2 and parts[0] and parts[1]:
        return {"name": f"{parts[0]}/{parts[1]}"}, SOURCE_MODULE_PATH
    return None


def _repository_url(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _component_repository_supplier(
    component: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """Derive a supplier from a repository URL the component already carries."""
    purl = component.get("purl")
    if isinstance(purl, str) and "vcs_url=" in purl:
        qualifier = purl.split("vcs_url=", 1)[1].split("&", 1)[0]
        chosen = supplier_from_path(unquote(qualifier))
        if chosen is not None:
            return chosen
    refs = component.get("externalReferences")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            url = ref.get("url")
            if isinstance(url, str):
                chosen = supplier_from_path(url)
                if chosen is not None:
                    return chosen
    return None


def _go_supplier(module_path: str) -> tuple[dict[str, Any], str] | None:
    return supplier_from_path(module_path)


def derive_supplier(
    component: dict[str, Any], index: MetadataIndex
) -> tuple[dict[str, Any], str] | None:
    """Derive one supplier from the best local source. Never invent a company.

    Args:
        component: A CycloneDX component object.
        index: Offline metadata collected from the build roots.

    Returns:
        ``(supplier object, source property)`` or ``None`` when nothing local
        names a supplier. The caller records ``unresolved`` in that case.
        A purl namespace is not used: the platform only counts
        ``supplier.name``, and a registry name would be an invented supplier.
    """
    purl = component.get("purl")
    parsed = parse_purl(purl) if isinstance(purl, str) else None
    name = component.get("name") if isinstance(component.get("name"), str) else ""
    if parsed is None:
        return None
    ecosystem, purl_name = parsed
    if ecosystem == "pypi":
        paths = index.python_metadata(purl_name or name)
        version = component.get("version")
        if isinstance(version, str) and version:
            matched = [path for path in paths if version in path.parent.name]
            if matched:
                paths = matched
        chosen = _python_supplier(paths)
        if chosen is not None:
            return chosen
        return _component_repository_supplier(component)
    if ecosystem == "npm":
        manifest = index.npm_manifest(purl_name or name)
        scoped = _npm_scope_supplier(purl_name or name)
        if manifest is not None:
            chosen = _npm_supplier(manifest)
            if chosen is not None:
                return chosen
        if scoped is not None:
            return scoped
        if manifest is not None:
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            repository = _repository_url(data.get("repository"))
            if repository is not None:
                chosen = supplier_from_path(repository)
                if chosen is not None:
                    return chosen
        return _component_repository_supplier(component)
    if ecosystem == "golang":
        return _go_supplier(purl_name or name)
    return None


def fill_component_suppliers(document: dict[str, Any], index: MetadataIndex) -> None:
    """Set ``supplier`` on every component that does not already have one."""
    for component in iter_components(document):
        if has_supplier(component):
            continue
        derived = derive_supplier(component, index)
        if derived is None:
            set_supplier_source(component, SOURCE_UNRESOLVED)
            continue
        supplier, source = derived
        component["supplier"] = supplier
        set_supplier_source(component, source)


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
    """Stamp SBOM files and print the quality table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sboms", nargs="+", type=Path, help="CycloneDX JSON files")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=REPO_ROOT / "pyproject.toml",
        help="PEP 621 file the author list is read from",
    )
    parser.add_argument(
        "--python-root",
        action="append",
        default=[],
        type=Path,
        help="installed venv or site-packages to read *.dist-info/METADATA from",
    )
    parser.add_argument(
        "--npm-root",
        action="append",
        default=[],
        type=Path,
        help="node_modules directory to read package.json author metadata from",
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

    index = MetadataIndex(args.python_root, args.npm_root)
    header = (
        f"{'sbom':<44}{'comps':>7}{'ver':>8}{'purl':>8}{'lic':>8}"
        f"{'suppl':>8}{'unres':>7}{'deps':>7}"
    )
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
            fill_component_suppliers(document, index)
            path.write_text(
                json.dumps(document, indent=2, sort_keys=False) + "\n",
                encoding="utf-8",
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
            f"{stats['unresolved']:>7}{stats['dependencies']:>7}"
        )
        if stats["unresolved"]:
            print(
                f"{path}: {stats['unresolved']} components have no derived supplier",
                file=sys.stderr,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

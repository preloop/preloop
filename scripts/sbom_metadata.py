#!/usr/bin/env python3
"""Stamp manufacturer metadata onto CycloneDX SBOMs and measure their quality.

Three jobs. The first two are driven by the NTIA minimum elements:

1. Fill the fields the generators leave empty. syft and the CycloneDX
   generators emit ``metadata.authors: null`` and no ``metadata.supplier``,
   which is an NTIA miss on every SBOM this repo would otherwise publish.
   The document supplier, manufacturer and root component come from
   ``pyproject.toml``. Every other component gets its own supplier from a
   local, offline source (installed ``METADATA``, ``package.json``, or the
   Go module path). ``author`` stays author. The property
   ``preloop:supplier_source`` records which derivation was used.
   A component that still has no declared ``licenses`` entry gets one when
   a local source names exactly one SPDX license or expression: Python
   ``METADATA`` (``License-Expression``, then ``License``, then one mapped
   ``Classifier``, then ``License-File``), npm ``package.json`` (``license``,
   or a one-element ``licenses`` array), a Go module ``LICENSE`` file in the
   module cache,
   or ``BSD-3-Clause`` for the Go standard library. The property
   ``preloop:license_source`` records which derivation was used. Anything
   ambiguous is left blank.

2. Print a quality table. The supplier column counts ``supplier.name``
   only, the same rule as ``python -m preloop.cra measure``. Author and
   publisher do not count. ``lic0`` and ``lic1`` are the share of
   components with a declared ``licenses`` entry before and after the
   license stamp. ``evidence.licenses`` does not count in those columns.

3. Mark declaration-only npm packages. An ``@types/*`` package, or a
   package whose name ends in ``-types``, gets ``preloop:types_only``
   set to ``true`` when its installed directory contains no runtime
   file. The release security audit may use that property as evidence
   that a git-range match through the package ``vcs_url`` does not
   describe code in the component. The property does not suppress a
   finding. A VEX statement does.

Usage:
    python scripts/sbom_metadata.py sbom/*.cdx.json
    python scripts/sbom_metadata.py --python-root /path/to/venv \\
        --npm-root frontend/node_modules \\
        --go-mod-cache "$(go env GOMODCACHE)" sbom/*.cdx.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from email import message_from_string
from email import policy
from email.message import Message
from pathlib import Path
from typing import Any
from typing import Iterator
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent

SUPPLIER = {
    "name": "Preloop",
    "url": ["https://preloop.ai"],
    "contact": [{"name": "Preloop Security", "email": "security@preloop.ai"}],
}

SUPPLIER_SOURCE_PROPERTY = "preloop:supplier_source"
LICENSE_SOURCE_PROPERTY = "preloop:license_source"
TYPES_ONLY_PROPERTY = "preloop:types_only"
SOURCE_PYPI_EXPRESSION = "pypi_license_expression"
SOURCE_PYPI_LICENSE = "pypi_license"
SOURCE_PYPI_CLASSIFIER = "pypi_classifier"
SOURCE_PYPI_FILE = "pypi_license_file"
SOURCE_NPM_LICENSE = "npm_license"
SOURCE_NPM_LICENSES = "npm_licenses"
SOURCE_GO_FILE = "go_module_license"
SOURCE_GO_STDLIB = "go_stdlib"
_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
_RUNTIME_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".wasm",
        ".node",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
    }
)
SOURCE_AUTHOR = "package_metadata_author"
SOURCE_MAINTAINER = "package_metadata_maintainer"
SOURCE_NPM_SCOPE = "npm_scope"
SOURCE_MODULE_PATH = "module_path"
SOURCE_UNRESOLVED = "unresolved"
SOURCE_MANUAL = "manual_override"
_GO_STDLIB = "BSD-3-Clause"
_LICENSE_TEXT_LIMIT = 16_384
_SPDX_TAG_RE = re.compile(
    r"^.*?SPDX-License-Identifier:\s*(.+?)\s*$",
    re.MULTILINE,
)
_SPDX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")
_LICENSE_FILENAME_RE = re.compile(
    r"^(?:license|licence|copying|unlicense)(?:[._-][a-z0-9]+)?$",
    re.IGNORECASE,
)


def _load_id_file(filename: str) -> frozenset[str]:
    """Load one identifier per line from a file next to this script."""
    path = Path(__file__).resolve().parent / filename
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            found.add(text)
    return frozenset(found)


SPDX_LICENSE_IDS = _load_id_file("spdx_license_ids.txt")
SPDX_LICENSE_EXCEPTIONS = _load_id_file("spdx_license_exceptions.txt")

# Trove classifiers that name exactly one SPDX id. Classifiers that only
# say "BSD", "GPL", "LGPL", or "Artistic" are omitted on purpose.
CLASSIFIER_TO_SPDX: dict[str, str] = {
    "License :: Aladdin Free Public License (AFPL)": "Aladdin",
    "License :: CC0 1.0 Universal (CC0 1.0) Public Domain Dedication": "CC0-1.0",
    "License :: CeCILL-B Free Software License Agreement (CECILL-B)": "CECILL-B",
    "License :: CeCILL-C Free Software License Agreement (CECILL-C)": "CECILL-C",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: Blue Oak Model License (BlueOak-1.0.0)": (
        "BlueOak-1.0.0"
    ),
    "License :: OSI Approved :: Boost Software License 1.0 (BSL-1.0)": "BSL-1.0",
    "License :: OSI Approved :: CEA CNRS Inria Logiciel Libre License, version 2.1 (CeCILL-2.1)": (
        "CECILL-2.1"
    ),
    "License :: OSI Approved :: CMU License (MIT-CMU)": "MIT-CMU",
    "License :: OSI Approved :: Common Development and Distribution License 1.0 (CDDL-1.0)": (
        "CDDL-1.0"
    ),
    "License :: OSI Approved :: Common Public License": "CPL-1.0",
    "License :: OSI Approved :: Eclipse Public License 1.0 (EPL-1.0)": "EPL-1.0",
    "License :: OSI Approved :: Eclipse Public License 2.0 (EPL-2.0)": "EPL-2.0",
    "License :: OSI Approved :: Educational Community License, Version 2.0 (ECL-2.0)": (
        "ECL-2.0"
    ),
    "License :: OSI Approved :: European Union Public Licence 1.0 (EUPL 1.0)": (
        "EUPL-1.0"
    ),
    "License :: OSI Approved :: European Union Public Licence 1.1 (EUPL 1.1)": (
        "EUPL-1.1"
    ),
    "License :: OSI Approved :: European Union Public Licence 1.2 (EUPL 1.2)": (
        "EUPL-1.2"
    ),
    "License :: OSI Approved :: GNU Affero General Public License v3": "AGPL-3.0-only",
    "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)": (
        "AGPL-3.0-or-later"
    ),
    "License :: OSI Approved :: GNU General Public License v2 (GPLv2)": "GPL-2.0-only",
    "License :: OSI Approved :: GNU General Public License v2 or later (GPLv2+)": (
        "GPL-2.0-or-later"
    ),
    "License :: OSI Approved :: GNU General Public License v3 (GPLv3)": "GPL-3.0-only",
    "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)": (
        "GPL-3.0-or-later"
    ),
    "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)": (
        "LGPL-3.0-only"
    ),
    "License :: OSI Approved :: GNU Lesser General Public License v3 or later (LGPLv3+)": (
        "LGPL-3.0-or-later"
    ),
    "License :: OSI Approved :: Historical Permission Notice and Disclaimer (HPND)": (
        "HPND"
    ),
    "License :: OSI Approved :: IBM Public License": "IPL-1.0",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: MIT No Attribution License (MIT-0)": "MIT-0",
    "License :: OSI Approved :: MirOS License (MirOS)": "MirOS",
    "License :: OSI Approved :: Mozilla Public License 1.0 (MPL)": "MPL-1.0",
    "License :: OSI Approved :: Mozilla Public License 1.1 (MPL 1.1)": "MPL-1.1",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "License :: OSI Approved :: Mulan Permissive Software License v2 (MulanPSL-2.0)": (
        "MulanPSL-2.0"
    ),
    "License :: OSI Approved :: NASA Open Source Agreement v1.3 (NASA-1.3)": "NASA-1.3",
    "License :: OSI Approved :: Open Software License 3.0 (OSL-3.0)": "OSL-3.0",
    "License :: OSI Approved :: PostgreSQL License": "PostgreSQL",
    "License :: OSI Approved :: Python License (CNRI Python License)": "CNRI-Python",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
    "License :: OSI Approved :: Qt Public License (QPL)": "QPL-1.0",
    "License :: OSI Approved :: SIL Open Font License 1.1 (OFL-1.1)": "OFL-1.1",
    "License :: OSI Approved :: The Unlicense (Unlicense)": "Unlicense",
    "License :: OSI Approved :: Universal Permissive License (UPL)": "UPL-1.0",
    "License :: OSI Approved :: University of Illinois/NCSA Open Source License": "NCSA",
    "License :: OSI Approved :: Vovida Software License 1.0": "VSL-1.0",
    "License :: OSI Approved :: W3C License": "W3C",
    "License :: OSI Approved :: Zero-Clause BSD (0BSD)": "0BSD",
    "License :: OSI Approved :: zlib/libpng License": "Zlib",
}

# Exact License-header prose that is one SPDX id and nothing else.
# Matching is on the collapsed, case-folded string, never a substring.
_LICENSE_ALIASES: dict[str, str] = {
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "mit license": "MIT",
    "the mit license": "MIT",
    "the mit license (mit)": "MIT",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "revised bsd license": "BSD-3-Clause",
    "2-clause bsd license": "BSD-2-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "mozilla public license 2.0": "MPL-2.0",
}

# (id, phrases that must all occur, phrases that must not occur)
_HEADER_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "MIT",
        (
            "Permission is hereby granted, free of charge, to any person obtaining a copy",
        ),
        ("you agree", "Contributor Agreement", "may not be used"),
    ),
    (
        "Apache-2.0",
        ("Apache License", "Version 2.0, January 2004"),
        (),
    ),
    (
        "Apache-2.0",
        ("Licensed under the Apache License, Version 2.0",),
        (),
    ),
    (
        "BSD-3-Clause",
        ("Redistribution and use in source and binary forms", "Neither the name"),
        ("All advertising materials",),
    ),
    (
        "BSD-1-Clause",
        (
            "Redistribution and use in source and binary forms",
            "Redistributions of source code",
        ),
        (
            "Redistributions in binary form",
            "Neither the name",
            "All advertising materials",
        ),
    ),
    (
        "BSD-2-Clause",
        (
            "Redistribution and use in source and binary forms",
            "Redistributions of source code",
            "Redistributions in binary form",
        ),
        ("Neither the name", "All advertising materials", "views and conclusions"),
    ),
    (
        "ISC",
        (
            "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted",
            "appear in all copies",
        ),
        (),
    ),
    (
        "0BSD",
        (
            "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted",
        ),
        ("appear in all copies",),
    ),
    ("MPL-2.0", ("Mozilla Public License Version 2.0",), ()),
    (
        "Unlicense",
        ("This is free and unencumbered software released into the public domain.",),
        (),
    ),
    ("BSL-1.0", ("Boost Software License - Version 1.0",), ()),
)

# These distributions publish no author and no repository URL in the files
# an offline scan can read. The names below are copied from text the package
# itself ships (aiodocker's description links github.com/aio-libs/aiodocker;
# py-key-value-aio's description links strawgate.com/py-key-value) or, for
# lighthouse-logger, from the first maintainer on its npm registry record.
# The published tarball does not carry that maintainer.
MANUAL_SUPPLIERS: dict[tuple[str, str], str] = {
    ("pypi", "aiodocker"): "aio-libs",
    ("pypi", "py-key-value-aio"): "strawgate.com/py-key-value",
    ("npm", "lighthouse-logger"): "paulirish",
}

_PERSON_RE = re.compile(
    r"^\s*(?P<name>[^<(]*?)\s*"
    r"(?:<(?P<email>[^>]+)>)?\s*"
    r"(?:\((?P<url>[^)]+)\))?\s*$"
)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)


def looks_like_email(value: str) -> bool:
    """True when ``value`` can be a CycloneDX ``idn-email``.

    One ``@``, no URI scheme, no whitespace. A homepage written as
    ``<http://example.test>`` fails this and must not be emitted as
    ``contact.email``.
    """
    if any(character.isspace() for character in value):
        return False
    if value.count("@") != 1:
        return False
    return _SCHEME_RE.match(value) is None and "://" not in value


def looks_like_url(value: str) -> bool:
    """True when ``value`` is a URL that belongs on ``supplier.url``."""
    return _SCHEME_RE.match(value) is not None


def _take_email_and_url(email: str, url: str) -> tuple[str, str]:
    """Move a non-email out of the email slot when it is a URL."""
    email = email.strip()
    url = url.strip()
    if email and not looks_like_email(email):
        if not url and looks_like_url(email):
            url = email
        email = ""
    return email, url


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


def has_declared_licence(component: dict[str, Any]) -> bool:
    """True when CycloneDX ``licenses`` is set.

    ``evidence.licenses`` is a detector's inference and does not count.
    The license stamp only fills components this function rejects.
    """
    licenses = component.get("licenses")
    if not isinstance(licenses, list):
        return False
    return any(isinstance(item, dict) and item for item in licenses)


def declared_licence_stats(document: dict[str, Any]) -> tuple[int, str]:
    """Return ``(count, percent)`` of components with a declared license."""
    components = list(iter_components(document))
    total = len(components)
    count = sum(1 for component in components if has_declared_licence(component))
    if not total:
        return 0, "n/a"
    return count, f"{(100.0 * count / total):.1f}%"


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
    email_address, url = _take_email_and_url(
        match.group("email") or "",
        match.group("url") or "",
    )
    if not name and email_address:
        name = email_address
    if not name and url:
        name = url
    if not name:
        return None
    person: dict[str, str] = {"name": name}
    if email_address:
        person["email"] = email_address
    if url:
        person["url"] = url
    return person


def _header_people(message: Message, field: str) -> list[dict[str, str]]:
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
    message = message_from_string(text, policy=policy.compat32)
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
    email, url = _take_email_and_url(person.get("email") or "", person.get("url") or "")
    if url:
        supplier["url"] = [url]
    contact: dict[str, str] = {}
    if person.get("name") and person["name"] != email and person["name"] != url:
        contact["name"] = person["name"]
    if email:
        contact["email"] = email
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


def component_property(component: dict[str, Any], name: str) -> str | None:
    """Return one CycloneDX property value, if this component has it."""
    properties = component.get("properties")
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == name:
            value = prop.get("value")
            if isinstance(value, str):
                return value
    return None


def supplier_source(component: dict[str, Any]) -> str | None:
    """Return the ``preloop:supplier_source`` value, if this script set one."""
    return component_property(component, SUPPLIER_SOURCE_PROPERTY)


def license_source(component: dict[str, Any]) -> str | None:
    """Return the ``preloop:license_source`` value, if this script set one."""
    return component_property(component, LICENSE_SOURCE_PROPERTY)


def set_component_property(component: dict[str, Any], name: str, value: str) -> None:
    """Set one CycloneDX property, replacing a previous value of the same name."""
    properties = component.get("properties")
    if not isinstance(properties, list):
        properties = []
        component["properties"] = properties
    for prop in properties:
        if isinstance(prop, dict) and prop.get("name") == name:
            prop["value"] = value
            return
    properties.append({"name": name, "value": value})


def set_supplier_source(component: dict[str, Any], source: str) -> None:
    """Record how ``supplier`` was chosen. Replace a previous stamp of ours."""
    set_component_property(component, SUPPLIER_SOURCE_PROPERTY, source)


def is_types_package_name(name: str) -> bool:
    """True for ``@types/*`` and for a package whose own name ends in ``-types``.

    Args:
        name: npm package name, including an optional scope.

    Returns:
        Whether the name is a declaration-package name. A scoped package
        matches on its own name (``@scope/widget-types``), not on the scope.
    """
    if name.startswith("@types/") and name != "@types/":
        return True
    leaf = name.rsplit("/", 1)[-1]
    return len(leaf) > len("-types") and leaf.endswith("-types")


def _is_runtime_file(path: Path) -> bool:
    """True when ``path`` can carry executable or runtime code.

    Declaration files are ignored. A known runtime suffix, an executable
    bit, or a ``#!`` prefix all count. An unreadable file counts too, so a
    types-only stamp is never a guess.
    """
    lowered = path.name.lower()
    if lowered.endswith(_DECLARATION_SUFFIXES):
        return False
    if path.suffix.lower() in _RUNTIME_SUFFIXES:
        return True
    try:
        mode = path.stat().st_mode
    except OSError:
        return True
    if mode & 0o111:
        return True
    try:
        with path.open("rb") as handle:
            prefix = handle.read(2)
    except OSError:
        return True
    return prefix == b"#!"


def directory_has_runtime_file(root: Path) -> bool:
    """True when ``root`` contains a file other than a TypeScript declaration.

    Nested ``node_modules`` are ignored. A missing directory is treated as
    having runtime files so a types-only stamp is never a guess. Extensionless
    files count when they are executable or start with a shebang.

    Args:
        root: Installed package directory.

    Returns:
        Whether a runtime file was found, or the directory could not be read.
    """
    if not root.is_dir():
        return True
    for _current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != "node_modules"]
        for filename in filenames:
            if _is_runtime_file(Path(_current) / filename):
                return True
    return False


def manifest_version(manifest: Path) -> str | None:
    """Return the ``version`` field of a ``package.json``, if it is a string."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def stamp_types_only(document: dict[str, Any], index: MetadataIndex) -> None:
    """Mark npm declaration packages that contain no runtime files.

    The property is ``preloop:types_only`` = ``true``. It is set only when
    the purl names an ``@types/*`` or ``*-types`` package, the installed
    directory has no runtime file, and the installed ``package.json``
    version matches the component version when both are present. A package
    that is not on disk, or whose installed version differs, is left unmarked.

    Args:
        document: CycloneDX document being stamped.
        index: Offline npm metadata collected from the build roots.
    """
    for component in iter_components(document):
        purl = component.get("purl")
        parsed = parse_purl(purl) if isinstance(purl, str) else None
        if parsed is None or parsed[0] != "npm":
            continue
        purl_name = parsed[1]
        if not is_types_package_name(purl_name):
            continue
        manifest = index.npm_manifest(purl_name)
        if manifest is None:
            continue
        installed = manifest_version(manifest)
        component_version = component.get("version")
        if (
            isinstance(component_version, str)
            and component_version.strip()
            and installed is not None
            and installed != component_version.strip()
        ):
            continue
        if directory_has_runtime_file(manifest.parent):
            continue
        set_component_property(component, TYPES_ONLY_PROPERTY, "true")


class MetadataIndex:
    """Offline lookup of installed Python, npm, and Go module metadata."""

    def __init__(
        self,
        python_roots: list[Path],
        npm_roots: list[Path],
        go_mod_cache: Path | None = None,
    ) -> None:
        self._python: dict[str, list[Path]] = {}
        self._npm: dict[str, Path] = {}
        self.go_mod_cache = go_mod_cache
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
                message = message_from_string(
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
        for manifest in _npm_manifests(root):
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
        email_text = email_address.strip() if isinstance(email_address, str) else ""
        url_text = url.strip() if isinstance(url, str) else ""
        email_text, url_text = _take_email_and_url(email_text, url_text)
        if isinstance(name, str) and name.strip():
            person["name"] = name.strip()
        if email_text:
            person["email"] = email_text
        if url_text:
            person["url"] = url_text
        if not person.get("name") and person.get("email"):
            person["name"] = person["email"]
        if not person.get("name") and person.get("url"):
            person["name"] = person["url"]
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
    maintainers = _npm_people(data.get("maintainers"))
    chosen = supplier_from_people(authors, maintainers)
    if chosen is not None:
        return chosen
    contributors = _npm_people(data.get("contributors"))
    return supplier_from_people(contributors, [])


def _npm_scope_supplier(name: str) -> tuple[dict[str, Any], str] | None:
    if not name.startswith("@") or "/" not in name:
        return None
    scope = name.split("/", 1)[0]
    if scope == "@":
        return None
    return {"name": scope}, SOURCE_NPM_SCOPE


def _npm_manifests(root: Path) -> Iterator[Path]:
    """Yield ``package.json`` files under ``root``, following directory symlinks.

    ``Path.rglob`` does not descend through symlinks. npm ``file:`` installs
    are symlinks (``node_modules/braces`` points at ``vendor/braces``), so a
    physical walk never sees the vendored manifest. Each resolved directory
    is entered once, which stops a symlink cycle from recursing forever.
    """
    pending = [root]
    seen: set[Path] = set()
    while pending:
        current = pending.pop()
        try:
            resolved = current.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    pending.append(entry)
                    continue
                if entry.name == "package.json" and entry.is_file():
                    yield entry
            except OSError:
                continue


def supplier_from_path(module_path: str) -> tuple[dict[str, Any], str] | None:
    """Apply the module-path rule to a Go module or a repository path.

    ``golang.org/x`` and ``std`` are The Go Authors. ``github.com/<org>``
    uses the org. Any other host uses the host plus the first path segment.
    A shorthand ``org/repo`` is treated as GitHub. Stacked ``git+`` and URL
    prefixes, including ``git+https://``, are removed in full so the host
    is parsed instead of a leftover ``https:`` name.
    """
    path = module_path.strip().strip("/")
    while True:
        stripped = re.sub(r"^(git\+|git:|https?://)", "", path)
        if stripped == path:
            break
        path = stripped
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
        chosen = _component_repository_supplier(component)
        if chosen is not None:
            return chosen
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
        chosen = _component_repository_supplier(component)
        if chosen is not None:
            return chosen
    elif ecosystem == "golang":
        chosen = _go_supplier(purl_name or name)
        if chosen is not None:
            return chosen
    manual = MANUAL_SUPPLIERS.get((ecosystem, purl_name or name))
    if manual:
        return {"name": manual}, SOURCE_MANUAL
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


def _id_choice(spdx_id: str) -> dict[str, Any]:
    return {"license": {"id": spdx_id}}


def _expression_choice(expression: str) -> dict[str, Any]:
    return {"expression": expression}


def _normalize_expression(tokens: list[str]) -> str:
    parts: list[str] = []
    for token in tokens:
        if token == ")":
            if parts:
                parts[-1] = f"{parts[-1]})"
            else:
                parts.append(")")
            continue
        if token == "(":
            parts.append("(")
            continue
        if parts and parts[-1].endswith("("):
            parts[-1] = f"{parts[-1]}{token}"
            continue
        parts.append(token)
    return " ".join(parts)


def _tokenize_spdx(text: str) -> list[str] | None:
    raw = text.replace("(", " ( ").replace(")", " ) ")
    tokens = raw.split()
    if not tokens:
        return None
    for token in tokens:
        if token in {"(", ")", "AND", "OR", "WITH"}:
            continue
        ident = token[:-1] if token.endswith("+") else token
        if _SPDX_ID_RE.fullmatch(ident) is None:
            return None
    return tokens


class _SpdxParser:
    """Recursive descent for a SPDX license expression."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._index = 0
        self.compound = False

    def parse(self) -> bool:
        """Return whether the tokens are one complete expression."""
        if not self._parse_or():
            return False
        return self._index == len(self._tokens)

    def _peek(self) -> str | None:
        if self._index >= len(self._tokens):
            return None
        return self._tokens[self._index]

    def _parse_or(self) -> bool:
        if not self._parse_and():
            return False
        while self._peek() == "OR":
            self.compound = True
            self._index += 1
            if not self._parse_and():
                return False
        return True

    def _parse_and(self) -> bool:
        if not self._parse_primary():
            return False
        while self._peek() == "AND":
            self.compound = True
            self._index += 1
            if not self._parse_primary():
                return False
        return True

    def _parse_primary(self) -> bool:
        token = self._peek()
        if token == "(":
            self.compound = True
            self._index += 1
            if not self._parse_or():
                return False
            if self._peek() != ")":
                return False
            self._index += 1
            return True
        if token is None or token in {")", "AND", "OR", "WITH"}:
            return False
        ident = token[:-1] if token.endswith("+") else token
        if token.endswith("+"):
            self.compound = True
        if ident not in SPDX_LICENSE_IDS:
            return False
        self._index += 1
        if self._peek() == "WITH":
            self.compound = True
            self._index += 1
            exception = self._peek()
            if exception is None or exception not in SPDX_LICENSE_EXCEPTIONS:
                return False
            self._index += 1
        return True


def spdx_choice(text: str) -> dict[str, Any] | None:
    """Return a CycloneDX license choice for one SPDX expression.

    A single license id becomes ``{"license": {"id": ...}}``. ``AND``,
    ``OR``, ``WITH``, parentheses, or a trailing ``+`` become
    ``{"expression": ...}``. An id that is not on the SPDX list is refused.

    Args:
        text: A candidate SPDX license expression.

    Returns:
        The license choice, or ``None`` when ``text`` is not a valid
        expression of known ids.
    """
    tokens = _tokenize_spdx(text.strip())
    if tokens is None:
        return None
    parser = _SpdxParser(tokens)
    if not parser.parse():
        return None
    if not parser.compound and len(tokens) == 1:
        return _id_choice(tokens[0])
    return _expression_choice(_normalize_expression(tokens))


def _alias_choice(text: str) -> dict[str, Any] | None:
    key = " ".join(text.split()).casefold()
    spdx_id = _LICENSE_ALIASES.get(key)
    if spdx_id is None or spdx_id not in SPDX_LICENSE_IDS:
        return None
    return _id_choice(spdx_id)


def _clean_spdx_tag(value: str) -> str:
    return re.sub(r"\s*\*/\s*$", "", value.strip()).strip()


def detect_license_text(text: str) -> dict[str, Any] | None:
    """Return one license choice from license-file text, or None.

    An ``SPDX-License-Identifier`` tag wins. Otherwise every well-known
    header that matches must name the same id. Dual-license wording and
    two different matches are left unrecognized.

    Args:
        text: License file or ``License`` header body.

    Returns:
        The license choice, or ``None`` when the text is not unambiguous.
    """
    tags = [_clean_spdx_tag(tag) for tag in _SPDX_TAG_RE.findall(text)]
    tags = [tag for tag in tags if tag]
    if tags:
        choices = [spdx_choice(tag) for tag in tags]
        if any(choice is None for choice in choices):
            return None
        first = choices[0]
        if any(choice != first for choice in choices):
            return None
        return first
    folded = " ".join(text.casefold().split())
    if "dual licen" in folded or "two different licen" in folded:
        return None
    matched: set[str] = set()
    for spdx_id, required, forbidden in _HEADER_RULES:
        if all(phrase.casefold() in folded for phrase in required) and not any(
            phrase.casefold() in folded for phrase in forbidden
        ):
            matched.add(spdx_id)
    if len(matched) != 1:
        return None
    spdx_id = next(iter(matched))
    if spdx_id not in SPDX_LICENSE_IDS:
        return None
    return _id_choice(spdx_id)


def _header_values(message: Message, field: str) -> list[str]:
    values: list[str] = []
    for raw in message.get_all(field, []):
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return values


def _blocks_classifier_fallback(value: str) -> bool:
    folded = " ".join(value.casefold().split())
    if "dual licen" in folded or "two different licen" in folded:
        return True
    if len(folded) <= 160 and re.search(r"\b(?:or|and)\b|,|;", folded):
        return True
    return False


def _classifier_choice(message: Message) -> tuple[dict[str, Any], str] | None:
    mapped: list[str] = []
    for value in _header_values(message, "Classifier"):
        spdx_id = CLASSIFIER_TO_SPDX.get(value.strip())
        if spdx_id and spdx_id in SPDX_LICENSE_IDS:
            mapped.append(spdx_id)
    unique = list(dict.fromkeys(mapped))
    if len(unique) != 1:
        return None
    return _id_choice(unique[0]), SOURCE_PYPI_CLASSIFIER


def license_from_metadata_text(text: str) -> tuple[dict[str, Any], str] | None:
    """Return a license choice from a METADATA or PKG-INFO body.

    ``License-Expression`` wins. A ``License`` value is used when it is an
    SPDX expression, an exact known alias, or a well-known license text.
    Classifiers are used only when they agree on one id and the ``License``
    field did not already name something this function cannot interpret.

    Args:
        text: The METADATA file body.

    Returns:
        ``(license choice, source)`` or ``None``.
    """
    message = message_from_string(text, policy=policy.compat32)
    expressions = _header_values(message, "License-Expression")
    if expressions:
        distinct = list(dict.fromkeys(expressions))
        if len(distinct) != 1:
            return None
        choice = spdx_choice(distinct[0])
        if choice is None:
            return None
        return choice, SOURCE_PYPI_EXPRESSION
    declared = _header_values(message, "License")
    distinct_declared = list(dict.fromkeys(declared))
    if len(distinct_declared) > 1:
        return None
    if len(distinct_declared) == 1:
        value = distinct_declared[0]
        choice = (
            spdx_choice(value) or _alias_choice(value) or detect_license_text(value)
        )
        if choice is not None:
            return choice, SOURCE_PYPI_LICENSE
        if len(" ".join(value.split())) > 160 or _blocks_classifier_fallback(value):
            return None
    return _classifier_choice(message)


def _npm_license_choice(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return spdx_choice(text)
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and kind.strip():
            return spdx_choice(kind.strip())
    return None


def license_from_package_json(
    data: dict[str, Any],
) -> tuple[dict[str, Any], str] | None:
    """Return a license choice from one package.json object.

    The current ``license`` field wins. The deprecated ``licenses`` array
    is used only when ``license`` is absent and the array has one entry.
    """
    if "license" in data and data.get("license") not in (None, ""):
        choice = _npm_license_choice(data.get("license"))
        if choice is None:
            return None
        return choice, SOURCE_NPM_LICENSE
    entries = data.get("licenses")
    if not isinstance(entries, list) or len(entries) != 1:
        return None
    choice = _npm_license_choice(entries[0])
    if choice is None:
        return None
    return choice, SOURCE_NPM_LICENSES


def _go_escape(value: str) -> str:
    """Escape a module path or version the way the Go module cache does."""
    return "".join(f"!{char.lower()}" if char.isupper() else char for char in value)


def _is_go_stdlib(module_path: str) -> bool:
    path = module_path.strip().strip("/")
    return path in {"std", "stdlib"} or path.startswith(("std/", "stdlib/"))


def purl_version(purl: str) -> str | None:
    """Return the version from a Package URL, if it has one."""
    if not purl.startswith("pkg:"):
        return None
    body = purl[4:].split("?", 1)[0].split("#", 1)[0]
    _ecosystem, sep, remainder = body.partition("/")
    if not sep:
        return None
    _name, at, version = remainder.rpartition("@")
    if not at or not version:
        return None
    return unquote(version)


def _go_module_dir(cache: Path, module_path: str, version: str) -> Path | None:
    parts = [part for part in module_path.split("/") if part]
    escaped_version = _go_escape(version)
    for length in range(len(parts), 0, -1):
        candidate = "/".join(parts[:length])
        directory = cache / f"{_go_escape(candidate)}@{escaped_version}"
        if directory.is_dir():
            return directory
    return None


def _license_files(directory: Path) -> list[Path]:
    found: list[Path] = []
    try:
        children = list(directory.iterdir())
    except OSError:
        return []
    for child in children:
        if child.is_file() and _LICENSE_FILENAME_RE.fullmatch(child.name):
            found.append(child)
    return sorted(found)


def _read_license_head(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(_LICENSE_TEXT_LIMIT)
    except OSError:
        return None


def _go_license_from_dir(directory: Path) -> dict[str, Any] | None:
    files = _license_files(directory)
    if not files:
        return None
    choices: list[dict[str, Any]] = []
    for path in files:
        text = _read_license_head(path)
        if text is None:
            return None
        if not text.strip():
            continue
        choice = detect_license_text(text)
        if choice is None:
            return None
        choices.append(choice)
    if not choices:
        return None
    first = choices[0]
    if any(choice != first for choice in choices):
        return None
    return first


def _may_use_license_files(text: str) -> bool:
    """True when METADATA did not already name a license we must not override."""
    message = message_from_string(text, policy=policy.compat32)
    if _header_values(message, "License-Expression"):
        return False
    declared = _header_values(message, "License")
    if len(dict.fromkeys(declared)) > 1:
        return False
    if len(declared) == 1:
        value = declared[0]
        if (
            spdx_choice(value) is not None
            or _alias_choice(value) is not None
            or detect_license_text(value) is not None
        ):
            return False
        if len(" ".join(value.split())) > 160 or _blocks_classifier_fallback(value):
            return False
    mapped: list[str] = []
    for value in _header_values(message, "Classifier"):
        spdx_id = CLASSIFIER_TO_SPDX.get(value.strip())
        if spdx_id and spdx_id in SPDX_LICENSE_IDS:
            mapped.append(spdx_id)
    if len(dict.fromkeys(mapped)) > 1:
        return False
    return True


def _safe_license_file(dist_info: Path, declared: str) -> Path | None:
    """Resolve one ``License-File`` path inside ``dist-info/licenses``."""
    raw = declared.strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (dist_info / "licenses" / relative).resolve()
    try:
        candidate.relative_to(dist_info.resolve())
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    return None


def _license_from_dist_info_files(
    metadata: Path, text: str
) -> tuple[dict[str, Any], str] | None:
    """Use ``License-File`` when the license texts agree on one license.

    A referenced file whose name is not a license filename (``NOTICE``,
    ``AUTHORS``) and whose text is not a known license is skipped. A
    license-named file that this stamp does not recognize fails the
    distribution closed.
    """
    if not _may_use_license_files(text):
        return None
    message = message_from_string(text, policy=policy.compat32)
    declared = _header_values(message, "License-File")
    if not declared:
        return None
    choices: list[dict[str, Any]] = []
    for value in declared:
        path = _safe_license_file(metadata.parent, value)
        if path is None:
            return None
        body = _read_license_head(path)
        if body is None or not body.strip():
            return None
        choice = detect_license_text(body)
        if choice is None:
            if _LICENSE_FILENAME_RE.fullmatch(path.name):
                return None
            continue
        choices.append(choice)
    if not choices:
        return None
    first = choices[0]
    if any(choice != first for choice in choices):
        return None
    return first, SOURCE_PYPI_FILE


def _python_metadata_paths(
    component: dict[str, Any], index: MetadataIndex, distribution: str
) -> list[Path]:
    """Return METADATA paths, preferring the component version when present."""
    paths = index.python_metadata(distribution)
    version = component.get("version")
    if isinstance(version, str) and version:
        matched = [path for path in paths if version in path.parent.name]
        if matched:
            return matched
    return paths


def derive_license(
    component: dict[str, Any], index: MetadataIndex
) -> tuple[dict[str, Any], str] | None:
    """Derive one declared license. Never guess.

    Args:
        component: A CycloneDX component object.
        index: Offline metadata collected from the build roots.

    Returns:
        ``(license choice, source property)`` or ``None`` when no local
        source names exactly one SPDX license or expression. The choice is
        either ``{"license": {"id": ...}}`` or ``{"expression": ...}``.
    """
    purl = component.get("purl")
    parsed = parse_purl(purl) if isinstance(purl, str) else None
    if parsed is None:
        return None
    ecosystem, purl_name = parsed
    name = component.get("name") if isinstance(component.get("name"), str) else ""
    if ecosystem == "pypi":
        for metadata in _python_metadata_paths(component, index, purl_name or name):
            try:
                text = metadata.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chosen = license_from_metadata_text(text)
            if chosen is not None:
                return chosen
            filed = _license_from_dist_info_files(metadata, text)
            if filed is not None:
                return filed
        return None
    if ecosystem == "npm":
        manifest = index.npm_manifest(purl_name or name)
        if manifest is None:
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return license_from_package_json(data)
    if ecosystem == "golang":
        module_path = purl_name or name
        if _is_go_stdlib(module_path):
            return _id_choice(_GO_STDLIB), SOURCE_GO_STDLIB
        cache = index.go_mod_cache
        if cache is None or not isinstance(purl, str):
            return None
        version = purl_version(purl)
        if not version:
            return None
        directory = _go_module_dir(cache, module_path, version)
        if directory is None:
            return None
        choice = _go_license_from_dir(directory)
        if choice is None:
            return None
        return choice, SOURCE_GO_FILE
    return None


def fill_component_licenses(document: dict[str, Any], index: MetadataIndex) -> None:
    """Set ``licenses`` on components that do not already declare one.

    Evidence-only detections are not a declaration and are filled when a
    local source is unambiguous. An ambiguous source is left blank, with
    no ``preloop:license_source`` property.

    Args:
        document: CycloneDX document being stamped.
        index: Offline metadata collected from the build roots.
    """
    for component in iter_components(document):
        if has_declared_licence(component):
            continue
        derived = derive_license(component, index)
        if derived is None:
            continue
        choice, source = derived
        component["licenses"] = [choice]
        set_component_property(component, LICENSE_SOURCE_PROPERTY, source)


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
        "--go-mod-cache",
        type=Path,
        default=None,
        help="Go module cache used to read LICENSE files (go env GOMODCACHE)",
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

    index = MetadataIndex(args.python_root, args.npm_root, args.go_mod_cache)
    header = (
        f"{'sbom':<44}{'comps':>7}{'ver':>8}{'purl':>8}{'lic0':>8}"
        f"{'lic1':>8}{'suppl':>8}{'unres':>7}{'deps':>7}"
    )
    print(header)
    print("-" * len(header))

    validator = build_validator() if args.validate else None

    failures = 0
    for path in args.sboms:
        document = json.loads(path.read_text(encoding="utf-8"))
        before_count, before_pct = declared_licence_stats(document)
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
            fill_component_licenses(document, index)
            stamp_types_only(document, index)
            path.write_text(
                json.dumps(document, indent=2, sort_keys=False) + "\n",
                encoding="utf-8",
            )
        after_count, after_pct = declared_licence_stats(document)

        if validator is not None:
            error = validator.validate_str(path.read_text(encoding="utf-8"))
            if error is not None:
                print(f"{path}: not valid CycloneDX 1.6: {error}", file=sys.stderr)
                failures += 1

        stats = measure(document)
        print(
            f"{path.name:<44}{stats['components']:>7}{stats['version']:>8}"
            f"{stats['purl']:>8}{before_pct:>8}{after_pct:>8}{stats['supplier']:>8}"
            f"{stats['unresolved']:>7}{stats['dependencies']:>7}"
        )
        print(
            f"{path.name}: licence coverage {before_pct} -> {after_pct} "
            f"({before_count}/{stats['components']} -> "
            f"{after_count}/{stats['components']})"
        )
        if stats["unresolved"]:
            print(
                f"{path}: {stats['unresolved']} components have no derived supplier",
                file=sys.stderr,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure NTIA / CRA minimum elements from delivered SBOM bytes.

The agent used to fill ``minimum_elements`` itself. A document author, or a
supplier on the root product component, was enough for it to report
``passed: true`` while every dependency component lacked a supplier. This
module is the platform's reading of the same bytes: a pure function of the
inputs, with no network and no look at the agent's claim.

CycloneDX JSON 1.4, 1.5 and 1.6, and SPDX JSON 2.2 and 2.3, are accepted.
Nested CycloneDX ``components`` and SPDX ``packages`` are counted. The
document's root product component (CycloneDX ``metadata.component``, SPDX
``documentDescribes`` / ``DESCRIBES``) is not part of the denominator.

Per component, ``author``, ``authors``, ``publisher`` and ``manufacturer``
are not a supplier. They are counted separately as author-only. A unique
identifier is a purl or a CPE. ``bom-ref`` and ``SPDXID`` do not count.
A relationship is the component appearing in CycloneDX ``dependencies`` or
in SPDX ``relationships``.

Replacing the agent's ``minimum_elements`` with this object is not rewriting
a measurement. The agent's field was a claim. This object is the measurement
of the bytes the run was given, and it is the authority for that claim.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

#: Persisted next to the agent's claim. The platform writes it; the agent does not.
MEASURED_FIELD = "minimum_elements_measured"

_ELEMENT_ORDER: tuple[str, ...] = (
    "supplier",
    "name",
    "version",
    "unique_identifier",
    "relationships",
    "document_author",
    "document_timestamp",
)

_CDX_VERSIONS = frozenset({"1.4", "1.5", "1.6"})
_SPDX_VERSIONS = frozenset({"SPDX-2.2", "SPDX-2.3"})
_MAX_SBOM_BYTES = 32 * 1024 * 1024
_GZIP_MAGIC = b"\x1f\x8b"
# Suffixes that promise a JSON SBOM. A file with one of these that does not
# parse still fails the aggregate. A path that merely contains "sbom", or a
# tag-value ``.spdx`` file, does not.
_PROMISED_JSON_SUFFIXES = (
    ".cdx.json",
    ".cyclonedx.json",
    ".spdx.json",
    "bom.json",
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _promised_json_sbom(path: str) -> bool:
    """True when the path itself promises CycloneDX or SPDX JSON."""
    return path.lower().endswith(_PROMISED_JSON_SUFFIXES)


def _skipped(
    reason: str,
    *,
    path: str = "",
    sha256: str = "",
    affects_passed: bool = False,
) -> dict[str, Any]:
    """A measurement that was not made, with the reason recorded.

    ``affects_passed`` is set only when the path promised a JSON SBOM, or the
    bytes parsed as one but the spec version is unsupported. A neighbour that
    is not an SBOM is recorded and does not fail the aggregate.
    """
    body: dict[str, Any] = {"status": "skipped", "reason": reason}
    if affects_passed:
        body["affects_passed"] = True
    if path:
        body["path"] = path
    if sha256:
        body["sha256"] = sha256
    return body


def _gunzip_bounded(raw: bytes) -> tuple[Optional[bytes], Optional[str]]:
    """Decompress ``raw``, stopping once the output exceeds the size cap."""
    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            while True:
                block = handle.read(65536)
                if not block:
                    break
                total += len(block)
                if total > _MAX_SBOM_BYTES:
                    return None, "decompressed input exceeds the measurement size cap"
                chunks.append(block)
    except (OSError, EOFError, gzip.BadGzipFile):
        return None, "gzip input could not be decompressed"
    return b"".join(chunks), None


def _decode_document(raw: bytes) -> tuple[Optional[Any], Optional[str]]:
    """Return parsed JSON, or a skip reason. Gzip inputs are decompressed."""
    if len(raw) > _MAX_SBOM_BYTES:
        return None, "input exceeds the measurement size cap"
    payload = raw
    if raw.startswith(_GZIP_MAGIC):
        payload_bytes, reason = _gunzip_bounded(raw)
        if reason is not None or payload_bytes is None:
            return None, reason or "gzip input could not be decompressed"
        payload = payload_bytes
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "input is not UTF-8 JSON"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "input is not valid JSON"


def _contact_named(value: Any) -> bool:
    if _nonempty(value):
        return True
    if isinstance(value, Mapping):
        return _nonempty(value.get("name"))
    return False


def _has_author_like(component: Mapping[str, Any]) -> bool:
    """True when an author-like field is present. These are not suppliers."""
    if _nonempty(component.get("author")):
        return True
    authors = component.get("authors")
    if isinstance(authors, list) and any(_contact_named(item) for item in authors):
        return True
    if _nonempty(component.get("publisher")):
        return True
    manufacturer = component.get("manufacturer")
    if _nonempty(manufacturer) or (
        isinstance(manufacturer, Mapping) and _nonempty(manufacturer.get("name"))
    ):
        return True
    return False


def _has_supplier_object(component: Mapping[str, Any]) -> bool:
    supplier = component.get("supplier")
    return isinstance(supplier, Mapping) and _nonempty(supplier.get("name"))


def _has_spdx_supplier(component: Mapping[str, Any]) -> bool:
    supplier = component.get("supplier")
    if not _nonempty(supplier):
        return False
    return str(supplier).strip() != "NOASSERTION"


def _has_cdx_identifier(component: Mapping[str, Any]) -> bool:
    if _nonempty(component.get("purl")):
        return True
    return _nonempty(component.get("cpe"))


def _has_spdx_identifier(component: Mapping[str, Any]) -> bool:
    refs = component.get("externalRefs")
    if not isinstance(refs, list):
        return False
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        kind = str(ref.get("referenceType") or "").lower()
        locator = ref.get("referenceLocator")
        if kind in {"purl", "cpe22type", "cpe23type"} and _nonempty(locator):
            if str(locator).strip() == "NOASSERTION":
                continue
            return True
    return False


def _tools_named(tools: Any) -> bool:
    if isinstance(tools, list):
        return any(_contact_named(item) for item in tools)
    if isinstance(tools, Mapping):
        for key in ("components", "services"):
            entries = tools.get(key)
            if isinstance(entries, list) and any(
                _contact_named(item) for item in entries
            ):
                return True
        return _contact_named(tools)
    return False


def _cdx_document_author(metadata: Mapping[str, Any]) -> bool:
    authors = metadata.get("authors")
    if isinstance(authors, list) and any(_contact_named(item) for item in authors):
        return True
    if _nonempty(metadata.get("author")):
        return True
    return _tools_named(metadata.get("tools"))


def _cdx_dependency_refs(document: Mapping[str, Any]) -> set[str]:
    refs: set[str] = set()
    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list):
        return refs
    for entry in dependencies:
        if not isinstance(entry, Mapping):
            continue
        ref = entry.get("ref")
        if isinstance(ref, str) and ref:
            refs.add(ref)
        depends_on = entry.get("dependsOn")
        if isinstance(depends_on, list):
            for item in depends_on:
                if isinstance(item, str) and item:
                    refs.add(item)
    return refs


def _iter_cdx_components(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Components except the metadata root product. Nested components count."""
    metadata = document.get("metadata")
    root: Optional[Mapping[str, Any]] = None
    if isinstance(metadata, Mapping) and isinstance(metadata.get("component"), Mapping):
        root = metadata["component"]
    root_ref = root.get("bom-ref") if isinstance(root, Mapping) else None
    seen: set[str] = set()
    found: list[Mapping[str, Any]] = []

    def walk(node: Any, *, is_root: bool) -> None:
        if not isinstance(node, Mapping):
            return
        ref = node.get("bom-ref")
        if isinstance(ref, str) and ref in seen:
            return
        if isinstance(ref, str):
            seen.add(ref)
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
                isinstance(node, Mapping)
                and isinstance(root_ref, str)
                and node.get("bom-ref") == root_ref
            )
            walk(node, is_root=is_root)
    if isinstance(root, Mapping):
        children = root.get("components")
        if isinstance(children, list):
            for child in children:
                walk(child, is_root=False)
    return found


def _blank_counts() -> dict[str, int]:
    return {key: 0 for key in _ELEMENT_ORDER}


def _tally_component(
    counts: dict[str, int],
    *,
    has_supplier: bool,
    has_author: bool,
    has_name: bool,
    has_version: bool,
    has_identifier: bool,
    has_relationship: bool,
) -> tuple[int, int]:
    """Return (author_only, missing_supplier_and_author) increments of 0 or 1."""
    author_only = 0
    neither = 0
    if not has_supplier:
        counts["supplier"] += 1
        if has_author:
            author_only = 1
        else:
            neither = 1
    if not has_name:
        counts["name"] += 1
    if not has_version:
        counts["version"] += 1
    if not has_identifier:
        counts["unique_identifier"] += 1
    if not has_relationship:
        counts["relationships"] += 1
    return author_only, neither


def _measure_cyclonedx(
    document: Mapping[str, Any], *, path: str, sha256: str
) -> dict[str, Any]:
    spec = document.get("specVersion")
    if not isinstance(spec, str) or spec not in _CDX_VERSIONS:
        return _skipped(
            f"unsupported CycloneDX spec version {spec!r}",
            path=path,
            sha256=sha256,
            affects_passed=True,
        )
    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    counts = _blank_counts()
    author_only = 0
    neither = 0
    related = _cdx_dependency_refs(document)
    components = _iter_cdx_components(document)
    for component in components:
        ref = component.get("bom-ref")
        added_author, added_neither = _tally_component(
            counts,
            has_supplier=_has_supplier_object(component),
            has_author=_has_author_like(component),
            has_name=_nonempty(component.get("name")),
            has_version=_nonempty(component.get("version")),
            has_identifier=_has_cdx_identifier(component),
            has_relationship=isinstance(ref, str) and ref in related,
        )
        author_only += added_author
        neither += added_neither
    if not _cdx_document_author(metadata):
        counts["document_author"] += 1
    if not _nonempty(metadata.get("timestamp")):
        counts["document_timestamp"] += 1
    return _document_result(
        counts,
        components=len(components),
        author_only=author_only,
        missing_supplier_and_author=neither,
        parser="cyclonedx",
        spec_version=spec,
        path=path,
        sha256=sha256,
    )


def _spdx_root_ids(document: Mapping[str, Any]) -> set[str]:
    roots: set[str] = set()
    described = document.get("documentDescribes")
    if isinstance(described, list):
        roots.update(item for item in described if isinstance(item, str) and item)
    relationships = document.get("relationships")
    if isinstance(relationships, list):
        for entry in relationships:
            if not isinstance(entry, Mapping):
                continue
            if entry.get("relationshipType") != "DESCRIBES":
                continue
            source = entry.get("spdxElementId")
            target = entry.get("relatedSpdxElement")
            if source in {"SPDXRef-DOCUMENT", "SPDXRef-Document"} and isinstance(
                target, str
            ):
                roots.add(target)
    return roots


def _spdx_related_ids(document: Mapping[str, Any]) -> set[str]:
    related: set[str] = set()
    relationships = document.get("relationships")
    if not isinstance(relationships, list):
        return related
    for entry in relationships:
        if not isinstance(entry, Mapping):
            continue
        for key in ("spdxElementId", "relatedSpdxElement"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                related.add(value)
    return related


def _measure_spdx(
    document: Mapping[str, Any], *, path: str, sha256: str
) -> dict[str, Any]:
    spec = document.get("spdxVersion")
    if not isinstance(spec, str) or spec not in _SPDX_VERSIONS:
        return _skipped(
            f"unsupported SPDX spec version {spec!r}",
            path=path,
            sha256=sha256,
            affects_passed=True,
        )
    creation = document.get("creationInfo")
    if not isinstance(creation, Mapping):
        creation = {}
    roots = _spdx_root_ids(document)
    related = _spdx_related_ids(document)
    counts = _blank_counts()
    author_only = 0
    neither = 0
    component_total = 0
    packages = document.get("packages")
    if isinstance(packages, list):
        for package in packages:
            if not isinstance(package, Mapping):
                continue
            spdx_id = package.get("SPDXID")
            if isinstance(spdx_id, str) and spdx_id in roots:
                continue
            component_total += 1
            added_author, added_neither = _tally_component(
                counts,
                has_supplier=_has_spdx_supplier(package),
                has_author=_has_author_like(package),
                has_name=_nonempty(package.get("name")),
                has_version=_nonempty(package.get("versionInfo")),
                has_identifier=_has_spdx_identifier(package),
                has_relationship=isinstance(spdx_id, str) and spdx_id in related,
            )
            author_only += added_author
            neither += added_neither
    creators = creation.get("creators")
    has_creator = isinstance(creators, list) and any(
        _nonempty(item) for item in creators
    )
    if not has_creator:
        counts["document_author"] += 1
    if not _nonempty(creation.get("created")):
        counts["document_timestamp"] += 1
    return _document_result(
        counts,
        components=component_total,
        author_only=author_only,
        missing_supplier_and_author=neither,
        parser="spdx",
        spec_version=spec,
        path=path,
        sha256=sha256,
    )


def _document_result(
    counts: dict[str, int],
    *,
    components: int,
    author_only: int,
    missing_supplier_and_author: int,
    parser: str,
    spec_version: str,
    path: str,
    sha256: str,
) -> dict[str, Any]:
    return {
        "path": path,
        "sha256": sha256,
        "parser": parser,
        "spec_version": spec_version,
        "components": components,
        "missing_counts": counts,
        "author_only": author_only,
        "missing_supplier_and_author": missing_supplier_and_author,
        "document_author": counts["document_author"] == 0,
        "document_timestamp": counts["document_timestamp"] == 0,
    }


def _classify(document: Mapping[str, Any]) -> Optional[str]:
    bom_format = document.get("bomFormat")
    if isinstance(bom_format, str) and bom_format.lower() == "cyclonedx":
        return "cyclonedx"
    if isinstance(document.get("spdxVersion"), str):
        return "spdx"
    if isinstance(document.get("specVersion"), str) and "components" in document:
        return "cyclonedx"
    return None


def measure_document(path: str, raw: bytes) -> dict[str, Any]:
    """Measure one SBOM. Unparseable input is skipped with a reason."""
    digest = _sha256(raw)
    promised = _promised_json_sbom(path)
    parsed, reason = _decode_document(raw)
    if reason is not None or not isinstance(parsed, Mapping):
        why = reason or "input is not a JSON object"
        return _skipped(why, path=path, sha256=digest, affects_passed=promised)
    kind = _classify(parsed)
    if kind == "cyclonedx":
        return _measure_cyclonedx(parsed, path=path, sha256=digest)
    if kind == "spdx":
        return _measure_spdx(parsed, path=path, sha256=digest)
    return _skipped(
        "input is not CycloneDX or SPDX JSON",
        path=path,
        sha256=digest,
        affects_passed=promised,
    )


def _missing_names(counts: Mapping[str, int]) -> list[str]:
    return [name for name in _ELEMENT_ORDER if int(counts.get(name, 0)) > 0]


def measure_inputs(inputs: Sequence[tuple[str, bytes]]) -> dict[str, Any]:
    """Measure one or more SBOM byte strings. Deterministic for the same bytes.

    Args:
        inputs: ``(path, bytes)`` pairs, in the order they should be reported.

    Returns:
        The measured object (``passed``, ``missing``, counts, per-document
        breakdown, parser, spec version, sha256) or a skipped object when
        nothing could be measured.
    """
    if not inputs:
        return _skipped("no SBOM seeds reachable")
    documents: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for path, raw in inputs:
        measured = measure_document(path, raw)
        if measured.get("status") == "skipped":
            skipped.append(measured)
        else:
            documents.append(measured)
    blocking = [item for item in skipped if item.get("affects_passed") is True]
    if not documents:
        if not blocking:
            body = _skipped("no parseable SBOM input")
            body["documents"] = skipped
            return body
        reason = (
            blocking[0]["reason"] if len(blocking) == 1 else "no parseable SBOM input"
        )
        # A path that promised JSON failed to parse. That is a measurement,
        # and it does not pass, rather than a skip that would keep the claim.
        failed = _aggregate([], blocking)
        failed["documents"] = skipped
        failed["passed"] = False
        failed["reason"] = reason
        return failed
    return _aggregate(documents, skipped)


def _aggregate(
    documents: Sequence[Mapping[str, Any]],
    skipped: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    counts = _blank_counts()
    components = 0
    author_only = 0
    neither = 0
    for document in documents:
        doc_counts = document.get("missing_counts")
        if isinstance(doc_counts, Mapping):
            for key in _ELEMENT_ORDER:
                counts[key] += int(doc_counts.get(key, 0))
        components += int(document.get("components") or 0)
        author_only += int(document.get("author_only") or 0)
        neither += int(document.get("missing_supplier_and_author") or 0)
    parsers = sorted({str(item.get("parser")) for item in documents})
    specs = sorted({str(item.get("spec_version")) for item in documents})
    blocking = [item for item in skipped if item.get("affects_passed") is True]
    passed = not _missing_names(counts) and not blocking
    if len(parsers) == 1:
        parser: Optional[str] = parsers[0]
        spec_version: Optional[str] = specs[0]
    elif parsers:
        parser = "mixed"
        spec_version = "mixed"
    else:
        parser = None
        spec_version = None
    return {
        "passed": passed,
        "missing": _missing_names(counts),
        "missing_counts": counts,
        "author_only": author_only,
        "missing_supplier_and_author": neither,
        "components": components,
        "parser": parser,
        "spec_version": spec_version,
        "documents": [*documents, *skipped],
    }


_UNMEASURED_FORMAT_REASON = (
    "SBOM seeds present in formats the platform does not measure "
    "(CycloneDX XML, SPDX tag-value)"
)


def _unmeasurable_sbom(path: str, raw: bytes) -> bool:
    """True for an SBOM shape this module does not parse.

    JSON SBOMs are measured. XML CycloneDX and SPDX tag-value are recorded
    so a delivery is not reported as "no seeds", and they do not fail
    ``passed`` (that would deny a complete JSON SBOM sitting beside them).
    """
    if _promised_json_sbom(path):
        return False
    lowered = path.lower()
    if lowered.endswith(".cdx.xml") or lowered.endswith(".spdx"):
        return True
    window = raw[:4096]
    if b"<" in window and b"cyclonedx" in window.lower():
        return True
    return _tag_value_header(window)


def _tag_value_header(raw: bytes) -> bool:
    """True when ``raw`` is SPDX tag-value, allowing leading comments.

    Tag-value permits ``#`` comment lines before ``SPDXVersion:``.
    """
    for line in raw.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith(b"#"):
            continue
        return stripped.startswith(b"SPDXVersion:")
    return False


def _looks_like_sbom(path: str, raw: bytes) -> bool:
    """True for a promised JSON SBOM path, or bytes that classify as one.

    A path that only contains the letters "sbom", and a tag-value ``.spdx``
    file, are not SBOMs. They must not sit next to a complete document and
    flip ``passed`` to false.
    """
    if _promised_json_sbom(path):
        return True
    parsed, reason = _decode_document(raw)
    if reason is not None or not isinstance(parsed, Mapping):
        return False
    return _classify(parsed) is not None


def measure_trigger(trigger_payload: Any) -> dict[str, Any]:
    """Measure SBOM seeds on a trigger payload. Missing seeds are skipped.

    Older trigger shapes and payloads with no SBOM bytes are not guessed.
    The returned object records why measurement did not run.
    """
    from preloop.utils.workspace_seed import (
        WorkspaceSeedError,
        parse_workspace_files,
        workspace_seed_payload,
    )

    try:
        container = workspace_seed_payload(
            dict(trigger_payload) if isinstance(trigger_payload, Mapping) else None
        )
    except WorkspaceSeedError as exc:
        return _skipped(f"workspace seeds unreadable: {exc}")
    if not isinstance(container, dict):
        return _skipped("no SBOM seeds reachable")
    try:
        seeds = parse_workspace_files(container)
    except WorkspaceSeedError as exc:
        return _skipped(f"workspace seeds unreadable: {exc}")
    inputs: list[tuple[str, bytes]] = []
    unmeasured: list[dict[str, Any]] = []
    for seed in seeds:
        try:
            raw = base64.b64decode(seed.content_base64, validate=True)
        except (ValueError, TypeError):
            continue
        if _looks_like_sbom(seed.path, raw):
            inputs.append((seed.path, raw))
        elif _unmeasurable_sbom(seed.path, raw):
            unmeasured.append(
                _skipped(
                    _UNMEASURED_FORMAT_REASON,
                    path=seed.path,
                    sha256=_sha256(raw),
                )
            )
    if not inputs and not unmeasured:
        return _skipped("no SBOM seeds reachable")
    if not inputs:
        body = _skipped(_UNMEASURED_FORMAT_REASON)
        body["documents"] = unmeasured
        return body
    measured = measure_inputs(inputs)
    documents = measured.get("documents")
    if isinstance(documents, list):
        documents.extend(unmeasured)
    elif unmeasured:
        measured["documents"] = unmeasured
    return measured


def place_measurement(payload: dict[str, Any], measurement: Mapping[str, Any]) -> None:
    """Attach ``measurement`` on the SBOM body the schema actually carries.

    Standalone SBOM audits keep it on the result. Release audits keep it on
    the nested ``sbom_audit``. An agent-authored value of the same key is
    overwritten: the platform is the writer.
    """
    from preloop.cra.schemas import SCHEMA_RELEASEAUDIT_V1, SCHEMA_SBOMAUDIT_V1

    schema = payload.get("schema")
    stamped = dict(measurement)
    if schema == SCHEMA_SBOMAUDIT_V1:
        payload[MEASURED_FIELD] = stamped
        return
    if schema == SCHEMA_RELEASEAUDIT_V1:
        sbom = payload.get("sbom_audit")
        if isinstance(sbom, dict):
            sbom[MEASURED_FIELD] = stamped
            return
    payload[MEASURED_FIELD] = stamped


def copy_measurement(source: Any, target: dict[str, Any]) -> None:
    """Copy a measurement already placed on ``source`` onto ``target``."""
    if not isinstance(source, Mapping):
        return
    direct = source.get(MEASURED_FIELD)
    if isinstance(direct, Mapping):
        target[MEASURED_FIELD] = dict(direct)
        return
    sbom = source.get("sbom_audit")
    if isinstance(sbom, Mapping) and isinstance(sbom.get(MEASURED_FIELD), Mapping):
        target[MEASURED_FIELD] = dict(sbom[MEASURED_FIELD])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the measured object for one or more SBOM files.

    Args:
        argv: Arguments after the ``measure`` subcommand.

    Returns:
        Process exit status. 0 when the object was printed.
    """
    parser = argparse.ArgumentParser(
        prog="python -m preloop.cra measure",
        description=(
            "Measure NTIA / CRA minimum elements for one or more SBOM files "
            "and print the object result.json should copy."
        ),
    )
    parser.add_argument("paths", nargs="+", help="CycloneDX or SPDX JSON files")
    args = parser.parse_args(list(argv) if argv is not None else None)
    prepared: list[tuple[str, bytes]] = []
    skips: list[dict[str, Any]] = []
    for path in args.paths:
        try:
            prepared.append((path, Path(path).read_bytes()))
        except OSError as exc:
            skips.append(_skipped(f"could not read file: {exc}", path=path))
    if not prepared and skips:
        body = _skipped(skips[0]["reason"])
        body["documents"] = skips
        json.dump(body, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    measured = measure_inputs(prepared)
    if skips and isinstance(measured.get("documents"), list):
        measured["documents"].extend(skips)
        measured["passed"] = False
    elif skips:
        measured = _skipped(skips[0]["reason"])
        measured["documents"] = skips
    json.dump(measured, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0

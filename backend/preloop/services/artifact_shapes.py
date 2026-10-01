"""Map session artifacts to and from MCP, A2A and OTel GenAI shapes.

No Preloop-specific wire shape: an artifact travels as

- MCP ``ContentBlock`` (spec 2026-07-28, ``schema/2026-07-28/schema.ts``,
  checked at modelcontextprotocol/modelcontextprotocol@046fa30e):
  ``ImageContent`` for screenshots, ``AudioContent`` for audio,
  ``EmbeddedResource`` (``text`` or ``blob``) for everything else, or a
  ``ResourceLink`` when only a URL is shared. Fields MCP has no slot for go
  in ``_meta["preloop.dev/artifact"]``.
- A2A v1.0.1 ``Artifact {artifactId, name, parts, metadata}`` with one
  ``Part {raw | url, filename, mediaType}`` (ProtoJSON names,
  ``specification/a2a.proto`` at a2aproject/A2A@1ae57a67). Kind, labels,
  sha256 and producer go in ``metadata["preloop.dev/artifact"]``.
- OTel GenAI ``UriPart`` (preferred) or ``BlobPart`` with ``modality``
  (``model/gen-ai/gen-ai-output-messages.json`` at
  open-telemetry/semantic-conventions-genai@b31e9e8e). The part schema has
  no metadata slot, so kind, labels, name and sha256 travel as span
  attributes ``preloop.artifact.*``.

Each ``from_*`` reverses its ``to_*``: content type, byte sha256, name, kind
and labels survive the round trip.

No production caller yet: the deposit route and MCP tools wire this up in
#1080 and #1081. Tests pin the mapping until then.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from preloop.services.artifact_media import KIND_MODALITY

META_KEY = "preloop.dev/artifact"
_TEXT_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/vtt",
        "application/x-subrip",
        "application/json",
    }
)


@dataclass
class ArtifactPayload:
    """Transport-neutral view of one artifact."""

    artifact_id: str
    kind: str
    content_type: str
    sha256: str
    name: str | None = None
    labels: dict[str, Any] = field(default_factory=dict)
    producer: str | None = None
    data: bytes | None = None
    size_bytes: int | None = None

    @classmethod
    def from_row(cls, row: Any, data: bytes | None = None) -> ArtifactPayload:
        """Build from a ``RuntimeSessionArtifact`` row and optional plaintext."""
        return cls(
            artifact_id=str(row.id),
            kind=row.kind,
            content_type=row.content_type,
            sha256=row.sha256,
            name=row.name,
            labels=dict(row.labels or {}),
            producer=row.producer,
            data=data,
            size_bytes=row.size_bytes,
        )


def _meta(a: ArtifactPayload) -> dict[str, Any]:
    return {
        "artifact_id": a.artifact_id,
        "kind": a.kind,
        "name": a.name,
        "labels": dict(a.labels),
        "sha256": a.sha256,
        "producer": a.producer,
    }


def _from_meta(
    meta: dict[str, Any],
    *,
    content_type: str,
    data: bytes | None,
    name: str | None = None,
    size_bytes: int | None = None,
) -> ArtifactPayload:
    sha256 = hashlib.sha256(data).hexdigest() if data is not None else meta["sha256"]
    return ArtifactPayload(
        artifact_id=str(meta.get("artifact_id") or ""),
        kind=meta["kind"],
        content_type=content_type,
        sha256=sha256,
        name=name if name is not None else meta.get("name"),
        labels=dict(meta.get("labels") or {}),
        producer=meta.get("producer"),
        data=data,
        size_bytes=len(data) if data is not None else size_bytes,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def to_mcp(a: ArtifactPayload, *, uri: str) -> dict[str, Any]:
    """Return an MCP ``ContentBlock`` carrying the bytes inline.

    Args:
        a: Artifact with ``data`` set.
        uri: Resource URI used by ``EmbeddedResource``.
    """
    if a.data is None:
        raise ValueError("artifact_unavailable")
    meta = {META_KEY: _meta(a)}
    if a.kind == "screenshot":
        return {
            "type": "image",
            "data": _b64(a.data),
            "mimeType": a.content_type,
            "_meta": meta,
        }
    if a.kind == "audio":
        return {
            "type": "audio",
            "data": _b64(a.data),
            "mimeType": a.content_type,
            "_meta": meta,
        }
    resource: dict[str, Any] = {"uri": uri, "mimeType": a.content_type}
    if a.content_type in _TEXT_TYPES:
        resource["text"] = a.data.decode("utf-8")
    else:
        resource["blob"] = _b64(a.data)
    return {"type": "resource", "resource": resource, "_meta": meta}


def to_mcp_link(a: ArtifactPayload, *, uri: str) -> dict[str, Any]:
    """Return an MCP ``ResourceLink`` pointing at the artifact bytes."""
    block: dict[str, Any] = {
        "type": "resource_link",
        "uri": uri,
        "name": a.name or a.artifact_id,
        "mimeType": a.content_type,
        "_meta": {META_KEY: _meta(a)},
    }
    if a.size_bytes is not None:
        block["size"] = a.size_bytes
    return block


def from_mcp(block: dict[str, Any]) -> ArtifactPayload:
    """Read an MCP ``ContentBlock`` produced by :func:`to_mcp` or a link."""
    meta = dict((block.get("_meta") or {}).get(META_KEY) or {})
    kind_default = {"image": "screenshot", "audio": "audio"}.get(block["type"])
    if kind_default and "kind" not in meta:
        meta["kind"] = kind_default
    if block["type"] in ("image", "audio"):
        data = base64.b64decode(block["data"])
        return _from_meta(meta, content_type=block["mimeType"], data=data)
    if block["type"] == "resource":
        resource = block["resource"]
        if "text" in resource:
            data = resource["text"].encode("utf-8")
        else:
            data = base64.b64decode(resource["blob"])
        meta.setdefault("kind", "generated_file")
        return _from_meta(meta, content_type=resource.get("mimeType", ""), data=data)
    if block["type"] == "resource_link":
        meta.setdefault("kind", "generated_file")
        return _from_meta(
            meta,
            content_type=block.get("mimeType", ""),
            data=None,
            name=meta.get("name") or block.get("name"),
            size_bytes=block.get("size"),
        )
    raise ValueError("artifact_content_block_unsupported")


def to_a2a(a: ArtifactPayload, *, url: str | None = None) -> dict[str, Any]:
    """Return an A2A ``Artifact`` with one ``raw`` part, or a ``url`` part."""
    part: dict[str, Any] = {"mediaType": a.content_type}
    if a.name:
        part["filename"] = a.name
    if url is not None:
        part["url"] = url
    elif a.data is not None:
        part["raw"] = _b64(a.data)
    else:
        raise ValueError("artifact_unavailable")
    artifact: dict[str, Any] = {
        "artifactId": a.artifact_id,
        "parts": [part],
        "metadata": {META_KEY: _meta(a)},
    }
    if a.name:
        artifact["name"] = a.name
    return artifact


def from_a2a(artifact: dict[str, Any]) -> ArtifactPayload:
    """Read an A2A ``Artifact`` with a single ``raw`` or ``url`` part."""
    meta = dict((artifact.get("metadata") or {}).get(META_KEY) or {})
    meta.setdefault("artifact_id", artifact.get("artifactId"))
    meta.setdefault("kind", "generated_file")
    part = artifact["parts"][0]
    data = base64.b64decode(part["raw"]) if "raw" in part else None
    return _from_meta(
        meta,
        content_type=part.get("mediaType", ""),
        data=data,
        name=artifact.get("name") or part.get("filename") or meta.get("name"),
    )


def to_otel(
    a: ArtifactPayload, *, uri: str | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return an OTel GenAI part and the span attributes that go with it.

    ``UriPart`` is used when ``uri`` is given (the spec discourages inline
    base64); otherwise a ``BlobPart``.

    Returns:
        ``(part, attributes)``. Attributes are strings so any exporter can
        carry them; labels are JSON.
    """
    modality = KIND_MODALITY[a.kind]
    if uri is not None:
        part: dict[str, Any] = {
            "type": "uri",
            "mime_type": a.content_type,
            "modality": modality,
            "uri": uri,
        }
    elif a.data is not None:
        part = {
            "type": "blob",
            "mime_type": a.content_type,
            "modality": modality,
            "content": _b64(a.data),
        }
    else:
        raise ValueError("artifact_unavailable")
    attributes = {
        "preloop.artifact.id": a.artifact_id,
        "preloop.artifact.kind": a.kind,
        "preloop.artifact.labels": json.dumps(a.labels, sort_keys=True),
        "preloop.artifact.sha256": a.sha256,
    }
    if a.name:
        attributes["preloop.artifact.name"] = a.name
    if a.producer:
        attributes["preloop.artifact.producer"] = a.producer
    return part, attributes


def from_otel(part: dict[str, Any], attributes: dict[str, str]) -> ArtifactPayload:
    """Read a part and span attributes produced by :func:`to_otel`."""
    meta = {
        "artifact_id": attributes.get("preloop.artifact.id"),
        "kind": attributes["preloop.artifact.kind"],
        "labels": json.loads(attributes.get("preloop.artifact.labels") or "{}"),
        "sha256": attributes.get("preloop.artifact.sha256"),
        "name": attributes.get("preloop.artifact.name"),
        "producer": attributes.get("preloop.artifact.producer"),
    }
    data = base64.b64decode(part["content"]) if part["type"] == "blob" else None
    return _from_meta(meta, content_type=part.get("mime_type") or "", data=data)

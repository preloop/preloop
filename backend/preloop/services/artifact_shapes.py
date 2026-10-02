"""Map session artifacts to and from MCP, A2A and OTel GenAI content shapes.

This module is pure: no database, no network, no settings. It is the one
place that knows the field names of the three standards Preloop speaks for
artifacts (product memo 2026-10-01, section B):

* MCP ``ContentBlock`` (spec 2026-07-28, ``schema/2026-07-28/schema.ts``,
  verified at modelcontextprotocol/modelcontextprotocol@046fa30e) is the
  ingest shape and the shape tools return.
* A2A v1.0.1 ``Part`` and ``Artifact`` (``specification/a2a.proto``,
  verified at a2aproject/A2A@33035925, tag v1.0.1) is the second import and
  export shape.
* OTel GenAI ``BlobPart``, ``UriPart`` and ``FilePart``
  (``model/gen-ai/gen-ai-output-messages.json``, status Development,
  verified at open-telemetry/semantic-conventions-genai@b31e9e8e) is the
  export shape for traces.

Preloop fields without a slot in a standard travel in that standard's
extension point: MCP ``_meta["preloop.dev/artifact"]``, A2A ``metadata``
under the same key, and OTel span attributes ``preloop.artifact.*``.

Errors are ``ValueError`` whose first argument is a stable code (see the
``ERROR_*`` constants), so callers can map them to row or HTTP errors.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

ERROR_BLOCK_TYPE_UNSUPPORTED = "artifact_block_type_unsupported"
ERROR_BLOCK_INVALID_BASE64 = "artifact_block_invalid_base64"
ERROR_BLOCK_TOO_LARGE = "artifact_block_too_large"

# ---------------------------------------------------------------------------
# Kinds and modalities
# ---------------------------------------------------------------------------

KIND_SCREENSHOT = "screenshot"
KIND_RECORDING = "recording"
KIND_SCREENCAST = "screencast"
KIND_AUDIO = "audio"
KIND_TRANSCRIPT = "transcript"
KIND_DOCUMENT = "document"
KIND_GENERATED_FILE = "generated_file"
KIND_TRACE = "trace"

MODALITY_IMAGE = "image"
MODALITY_VIDEO = "video"
MODALITY_AUDIO = "audio"
MODALITY_DOCUMENT = "document"

_KIND_MODALITY: dict[str, str] = {
    KIND_SCREENSHOT: MODALITY_IMAGE,
    KIND_RECORDING: MODALITY_VIDEO,
    KIND_SCREENCAST: MODALITY_VIDEO,
    KIND_AUDIO: MODALITY_AUDIO,
    KIND_TRANSCRIPT: MODALITY_DOCUMENT,
    KIND_DOCUMENT: MODALITY_DOCUMENT,
    KIND_GENERATED_FILE: MODALITY_DOCUMENT,
    KIND_TRACE: MODALITY_DOCUMENT,
}

TRANSCRIPT_CONTENT_TYPES = frozenset({"text/vtt", "application/x-subrip"})

DEFAULT_TEXT_CONTENT_TYPE = "text/plain"
DEFAULT_BINARY_CONTENT_TYPE = "application/octet-stream"

#: Decoded size cap used when the caller passes none. Per-kind caps belong
#: to the persistence layer (#1079, #1080), which should pass its own.
DEFAULT_MAX_DECODED_BYTES = 32 * 1024 * 1024

# ---------------------------------------------------------------------------
# Standard field names. OTel GenAI is status Development, so every name the
# module reads or writes lives here: a rename upstream is one edit.
# ---------------------------------------------------------------------------

META_KEY = "preloop.dev/artifact"

MCP_TYPE_TEXT = "text"
MCP_TYPE_IMAGE = "image"
MCP_TYPE_AUDIO = "audio"
MCP_TYPE_RESOURCE_LINK = "resource_link"
MCP_TYPE_RESOURCE = "resource"

A2A_TEXT = "text"
A2A_RAW = "raw"
A2A_URL = "url"
A2A_DATA = "data"
A2A_FILENAME = "filename"
A2A_MEDIA_TYPE = "media_type"
A2A_MEDIA_TYPE_JSON = "mediaType"  # ProtoJSON lowerCamel form, accepted on read
A2A_METADATA = "metadata"
A2A_ARTIFACT_ID = "artifact_id"
A2A_ARTIFACT_ID_JSON = "artifactId"
A2A_NAME = "name"
A2A_PARTS = "parts"

OTEL_TYPE = "type"
OTEL_TYPE_BLOB = "blob"
OTEL_TYPE_URI = "uri"
OTEL_TYPE_FILE = "file"
OTEL_MIME_TYPE = "mime_type"
OTEL_MODALITY = "modality"
OTEL_CONTENT = "content"
OTEL_URI = "uri"
OTEL_FILE_ID = "file_id"

OTEL_ATTR_KIND = "preloop.artifact.kind"
OTEL_ATTR_LABELS = "preloop.artifact.labels"
OTEL_ATTR_NAME = "preloop.artifact.name"
OTEL_ATTR_SHA256 = "preloop.artifact.sha256"

LABEL_PROVIDER_FILE_ID = "provider_file_id"


@dataclass(frozen=True)
class ArtifactPayload:
    """One artifact's content and the Preloop fields that travel with it.

    At most one of ``data`` and ``text`` is set. ``uri`` may accompany
    either, or stand alone for a reference. ``sha256`` is the hex digest of
    the bytes (``text`` is hashed as UTF-8); for a reference without bytes
    it is whatever the source declared, or ``None``.
    """

    kind: str
    name: str | None
    content_type: str
    data: bytes | None = None
    uri: str | None = None
    text: str | None = None
    labels: dict[str, Any] = field(default_factory=dict)
    sha256: str | None = None


def content_sha256(data: bytes | None, text: str | None) -> str | None:
    """Return the hex sha256 of ``data``, or of ``text`` as UTF-8."""
    if data is not None:
        return hashlib.sha256(data).hexdigest()
    if text is not None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    return None


def make_payload(
    *,
    kind: str | None,
    name: str | None,
    content_type: str | None,
    data: bytes | None = None,
    uri: str | None = None,
    text: str | None = None,
    labels: Mapping[str, Any] | None = None,
    sha256: str | None = None,
) -> ArtifactPayload:
    """Build a payload, inferring ``kind`` and computing ``sha256``.

    The digest is computed from the content when there is any; a declared
    ``sha256`` is only kept for references that carry no bytes.
    """
    if content_type is None:
        content_type = (
            DEFAULT_TEXT_CONTENT_TYPE
            if text is not None
            else DEFAULT_BINARY_CONTENT_TYPE
        )
    content_type = content_type.strip().lower() or DEFAULT_BINARY_CONTENT_TYPE
    if data is not None and text is None and _is_textual(content_type):
        try:
            text, data = data.decode("utf-8"), None
        except UnicodeDecodeError:
            pass
    digest = content_sha256(data, text) or sha256
    return ArtifactPayload(
        kind=kind or infer_kind(content_type),
        name=name or None,
        content_type=content_type,
        data=data,
        uri=uri or None,
        text=text,
        labels=dict(labels or {}),
        sha256=digest,
    )


def infer_kind(content_type: str, block_type: str | None = None) -> str:
    """Infer an artifact kind from an MCP block type and media type.

    image -> screenshot, audio -> audio, video -> recording, text/vtt or application/x-subrip ->
    transcript, other text -> document, anything else -> generated_file.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if block_type == MCP_TYPE_IMAGE or ct.startswith("image/"):
        return KIND_SCREENSHOT
    if block_type == MCP_TYPE_AUDIO or ct.startswith("audio/"):
        return KIND_AUDIO
    if ct.startswith("video/"):
        return KIND_RECORDING
    if ct in TRANSCRIPT_CONTENT_TYPES:
        return KIND_TRANSCRIPT
    if block_type == MCP_TYPE_TEXT or _is_textual(ct):
        return KIND_DOCUMENT
    return KIND_GENERATED_FILE


def modality_for(kind: str, content_type: str | None = None) -> str:
    """Return the OTel GenAI modality for an artifact.

    The kind decides; for an unknown kind the media type's top-level type
    does (image, video, audio), else ``document``.
    """
    if kind in _KIND_MODALITY:
        return _KIND_MODALITY[kind]
    top = (content_type or "").split("/", 1)[0].strip().lower()
    if top in (MODALITY_IMAGE, MODALITY_VIDEO, MODALITY_AUDIO):
        return top
    return MODALITY_DOCUMENT


def decode_base64(
    encoded: Any,
    *,
    max_bytes: int = DEFAULT_MAX_DECODED_BYTES,
    urlsafe: bool = False,
) -> bytes:
    """Decode strict base64, refusing oversize input before decoding.

    Mirrors ``browser_steps.decode_screenshot``: base64 carries 3 bytes per
    4 characters, so input longer than the encoding of ``max_bytes`` cannot
    decode to an allowed size and is refused without allocating a copy.

    ``urlsafe`` also accepts the URL-safe alphabet and missing padding, as
    the ProtoJSON mapping of ``bytes`` requires (A2A ``Part.raw``). MCP
    ``data`` and ``blob`` stay strict standard base64.

    Raises:
        ValueError: ``artifact_block_too_large`` or
            ``artifact_block_invalid_base64``.
    """
    if not isinstance(encoded, str):
        raise ValueError(ERROR_BLOCK_INVALID_BASE64)
    encoded = encoded.strip()
    if len(encoded) > 4 * ((max_bytes + 2) // 3):
        raise ValueError(ERROR_BLOCK_TOO_LARGE)
    if urlsafe:
        encoded = encoded.replace("-", "+").replace("_", "/")
        encoded += "=" * (-len(encoded) % 4)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(ERROR_BLOCK_INVALID_BASE64) from exc
    if len(data) > max_bytes:
        raise ValueError(ERROR_BLOCK_TOO_LARGE)
    return data


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _is_textual(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    return (
        ct.startswith("text/")
        or ct in TRANSCRIPT_CONTENT_TYPES
        or ct in ("application/json", "application/xml", "application/yaml")
        or ct.endswith("+json")
        or ct.endswith("+xml")
    )


def _content_bytes(p: ArtifactPayload) -> bytes | None:
    if p.data is not None:
        return p.data
    if p.text is not None:
        return p.text.encode("utf-8")
    return None


def _preloop_meta(source: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    meta = source.get(META_KEY)
    return dict(meta) if isinstance(meta, Mapping) else {}


def _meta_object(
    p: ArtifactPayload,
    *,
    artifact_id: str | None,
    producer: str | None,
    with_name: bool,
    with_content_type: bool,
    uri: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "artifact_id": artifact_id,
        "kind": p.kind,
        "labels": dict(p.labels),
        "sha256": p.sha256,
        "producer": producer,
    }
    # Blocks with no name or mimeType slot carry them here so a round trip
    # through MCP keeps them.
    if with_name and p.name is not None:
        meta["name"] = p.name
    if with_content_type:
        meta["content_type"] = p.content_type
    if uri:
        meta["uri"] = uri
    return meta


# ---------------------------------------------------------------------------
# MCP ContentBlock
# ---------------------------------------------------------------------------


def from_mcp_content_block(
    block: Mapping[str, Any],
    *,
    kind: str | None = None,
    max_bytes: int = DEFAULT_MAX_DECODED_BYTES,
) -> ArtifactPayload:
    """Build a payload from one MCP ``ContentBlock`` (as a dict).

    Accepts ``text``, ``image``, ``audio``, ``resource_link`` and
    ``resource`` (text or blob). ``kind`` wins over
    ``_meta["preloop.dev/artifact"].kind``, which wins over inference.

    Raises:
        ValueError: ``artifact_block_type_unsupported``,
            ``artifact_block_invalid_base64`` or ``artifact_block_too_large``.
    """
    if not isinstance(block, Mapping):
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    block_type = block.get("type")
    meta = _preloop_meta(block.get("_meta"))
    labels = meta.get("labels") if isinstance(meta.get("labels"), Mapping) else {}
    name = meta.get("name")
    content_type = meta.get("content_type")
    data: bytes | None = None
    text: str | None = None
    uri: str | None = meta.get("uri") if isinstance(meta.get("uri"), str) else None

    if block_type == MCP_TYPE_TEXT:
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
        content_type = content_type or DEFAULT_TEXT_CONTENT_TYPE
    elif block_type in (MCP_TYPE_IMAGE, MCP_TYPE_AUDIO):
        data = decode_base64(block.get("data"), max_bytes=max_bytes)
        content_type = block.get("mimeType") or content_type
    elif block_type == MCP_TYPE_RESOURCE_LINK:
        uri = block.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
        name = block.get("name") or name
        content_type = block.get("mimeType") or content_type
    elif block_type == MCP_TYPE_RESOURCE:
        resource = block.get("resource")
        if not isinstance(resource, Mapping):
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
        uri = resource.get("uri") or None
        content_type = resource.get("mimeType") or content_type
        if isinstance(resource.get("text"), str):
            text = resource["text"]
            content_type = content_type or DEFAULT_TEXT_CONTENT_TYPE
        elif "blob" in resource:
            data = decode_base64(resource.get("blob"), max_bytes=max_bytes)
        else:
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    else:
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)

    content_type = content_type or (
        DEFAULT_TEXT_CONTENT_TYPE if text is not None else DEFAULT_BINARY_CONTENT_TYPE
    )
    return make_payload(
        kind=kind or meta.get("kind") or infer_kind(content_type, block_type),
        name=name,
        content_type=content_type,
        data=data,
        uri=uri,
        text=text,
        labels=labels,
        sha256=meta.get("sha256"),
    )


def to_mcp_content_block(
    p: ArtifactPayload,
    *,
    uri: str | None = None,
    inline: bool = False,
    artifact_id: str | None = None,
    producer: str | None = None,
) -> dict[str, Any]:
    """Render a payload as one MCP ``ContentBlock`` dict.

    ``ResourceLink`` when a ``uri`` is given and ``inline`` is false, or
    when the payload has no bytes to inline. Otherwise the inline block:
    ``ImageContent`` / ``AudioContent`` for image or audio bytes,
    ``TextContent`` for ``text/plain`` text without a uri, and
    ``EmbeddedResource`` (text or blob) for the rest. Always sets
    ``_meta["preloop.dev/artifact"]``; ``ImageContent`` and
    ``AudioContent`` have no uri slot, so a given uri travels there.
    """
    link_uri = uri or p.uri
    content = _content_bytes(p)
    if (link_uri and not inline) or content is None:
        if not link_uri:
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
        block: dict[str, Any] = {
            "type": MCP_TYPE_RESOURCE_LINK,
            "uri": link_uri,
            "name": p.name or link_uri,
            "mimeType": p.content_type,
        }
        if content is not None:
            block["size"] = len(content)
        block["_meta"] = {
            META_KEY: _meta_object(
                p,
                artifact_id=artifact_id,
                producer=producer,
                with_name=False,
                with_content_type=False,
            )
        }
        return block

    top = p.content_type.split("/", 1)[0]
    if p.data is not None and top in (MCP_TYPE_IMAGE, MCP_TYPE_AUDIO):
        block = {"type": top, "data": _b64(p.data), "mimeType": p.content_type}
        with_ct = False
        meta_uri = link_uri
    elif (
        p.text is not None
        and not link_uri
        and p.content_type == DEFAULT_TEXT_CONTENT_TYPE
    ):
        block = {"type": MCP_TYPE_TEXT, "text": p.text}
        with_ct = False
        meta_uri = None
    elif link_uri:
        resource: dict[str, Any] = {"uri": link_uri, "mimeType": p.content_type}
        if p.text is not None:
            resource["text"] = p.text
        else:
            resource["blob"] = _b64(content)
        block = {"type": MCP_TYPE_RESOURCE, "resource": resource}
        with_ct = False
        meta_uri = None
    elif p.text is not None:
        # No uri to embed under and not plain text: TextContent has no
        # mimeType slot, so the media type travels in _meta.
        block = {"type": MCP_TYPE_TEXT, "text": p.text}
        with_ct = True
        meta_uri = None
    else:
        # Bytes that are neither image nor audio need a uri for an
        # EmbeddedResource; without one the caller must link instead.
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    block["_meta"] = {
        META_KEY: _meta_object(
            p,
            artifact_id=artifact_id,
            producer=producer,
            with_name=True,
            with_content_type=with_ct,
            uri=meta_uri,
        )
    }
    return block


# ---------------------------------------------------------------------------
# A2A Part / Artifact
# ---------------------------------------------------------------------------


def to_a2a_part(p: ArtifactPayload) -> dict[str, Any]:
    """Render a payload as an A2A v1 ``Part`` (ProtoJSON, proto field names).

    Text -> ``text``; bytes -> ``raw`` (base64, the ProtoJSON form of
    ``bytes``); reference only -> ``url``. ``filename`` and ``media_type``
    are always set when known; kind, labels and sha256 go to ``metadata``.
    """
    part: dict[str, Any] = {}
    if p.text is not None:
        part[A2A_TEXT] = p.text
    elif p.data is not None:
        part[A2A_RAW] = _b64(p.data)
    elif p.uri:
        part[A2A_URL] = p.uri
    else:
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    if p.name:
        part[A2A_FILENAME] = p.name
    part[A2A_MEDIA_TYPE] = p.content_type
    part[A2A_METADATA] = {
        META_KEY: {"kind": p.kind, "labels": dict(p.labels), "sha256": p.sha256}
    }
    return part


def from_a2a_part(
    part: Mapping[str, Any],
    *,
    kind: str | None = None,
    max_bytes: int = DEFAULT_MAX_DECODED_BYTES,
) -> ArtifactPayload:
    """Build a payload from an A2A ``Part`` dict.

    Reads proto field names and their ProtoJSON lowerCamel forms. ``raw``
    may be bytes or a base64 string. A ``data`` part (JSON value) becomes
    ``application/json`` text.
    """
    if not isinstance(part, Mapping):
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    meta = _preloop_meta(part.get(A2A_METADATA))
    content_type = part.get(A2A_MEDIA_TYPE) or part.get(A2A_MEDIA_TYPE_JSON)
    data: bytes | None = None
    text: str | None = None
    uri: str | None = None
    if isinstance(part.get(A2A_TEXT), str):
        text = part[A2A_TEXT]
    elif A2A_RAW in part:
        raw = part[A2A_RAW]
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > max_bytes:
                raise ValueError(ERROR_BLOCK_TOO_LARGE)
            data = bytes(raw)
        else:
            data = decode_base64(raw, max_bytes=max_bytes, urlsafe=True)
    elif isinstance(part.get(A2A_URL), str):
        uri = part[A2A_URL]
    elif A2A_DATA in part:
        text = json.dumps(part[A2A_DATA], separators=(",", ":"), sort_keys=True)
        content_type = content_type or "application/json"
    else:
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    labels = meta.get("labels") if isinstance(meta.get("labels"), Mapping) else {}
    return make_payload(
        kind=kind or meta.get("kind"),
        name=part.get(A2A_FILENAME),
        content_type=content_type,
        data=data,
        uri=uri,
        text=text,
        labels=labels,
        sha256=meta.get("sha256"),
    )


def to_a2a_artifact(
    artifact_id: str,
    name: str | None,
    payloads: Iterable[ArtifactPayload],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Render payloads as one A2A ``Artifact`` with one ``Part`` each."""
    artifact: dict[str, Any] = {
        A2A_ARTIFACT_ID: artifact_id,
        A2A_PARTS: [to_a2a_part(p) for p in payloads],
    }
    if name:
        artifact[A2A_NAME] = name
    if metadata:
        artifact[A2A_METADATA] = dict(metadata)
    return artifact


def from_a2a_artifact(
    artifact: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MAX_DECODED_BYTES,
) -> tuple[str, str | None, list[ArtifactPayload]]:
    """Read an A2A ``Artifact`` dict into ``(artifact_id, name, payloads)``.

    Accepts ``artifact_id`` or its ProtoJSON form ``artifactId``. Both id and
    a non-empty ``parts`` list are required by the proto.
    """
    if not isinstance(artifact, Mapping):
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    artifact_id = artifact.get(A2A_ARTIFACT_ID) or artifact.get(A2A_ARTIFACT_ID_JSON)
    parts = artifact.get(A2A_PARTS)
    if (
        not isinstance(artifact_id, str)
        or not artifact_id
        or not isinstance(parts, list)
        or not parts
    ):
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    return (
        artifact_id,
        artifact.get(A2A_NAME) or None,
        [from_a2a_part(part, max_bytes=max_bytes) for part in parts],
    )


# ---------------------------------------------------------------------------
# OTel GenAI parts
# ---------------------------------------------------------------------------


def otel_span_attributes(p: ArtifactPayload) -> dict[str, str]:
    """Return the span attributes that carry what OTel parts cannot.

    The GenAI Part schema has no metadata slot, so kind, labels (JSON),
    name and sha256 ride as ``preloop.artifact.*`` attributes.
    """
    attrs = {
        OTEL_ATTR_KIND: p.kind,
        OTEL_ATTR_LABELS: json.dumps(p.labels, sort_keys=True, separators=(",", ":")),
    }
    if p.name:
        attrs[OTEL_ATTR_NAME] = p.name
    if p.sha256:
        attrs[OTEL_ATTR_SHA256] = p.sha256
    return attrs


def to_otel_part(p: ArtifactPayload, *, uri: str | None = None) -> dict[str, Any]:
    """Render a payload as an OTel GenAI part.

    ``UriPart`` when ``uri`` is given or the payload has one (preferred: the spec discourages
    base64 data URLs), else ``BlobPart`` with base64 ``content``.
    """
    part: dict[str, Any] = {
        OTEL_MIME_TYPE: p.content_type,
        OTEL_MODALITY: modality_for(p.kind, p.content_type),
    }
    uri = uri or p.uri
    if uri:
        return {OTEL_TYPE: OTEL_TYPE_URI, **part, OTEL_URI: uri}
    content = _content_bytes(p)
    if content is None:
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    return {OTEL_TYPE: OTEL_TYPE_BLOB, **part, OTEL_CONTENT: _b64(content)}


def from_otel_part(
    part: Mapping[str, Any],
    *,
    attributes: Mapping[str, Any] | None = None,
    kind: str | None = None,
    max_bytes: int = DEFAULT_MAX_DECODED_BYTES,
) -> ArtifactPayload:
    """Build a payload from an OTel GenAI ``BlobPart``, ``UriPart`` or ``FilePart``.

    ``attributes`` are the span attributes from :func:`otel_span_attributes`.
    A ``FilePart`` yields no bytes and no uri; its provider ``file_id`` is
    kept in labels under ``provider_file_id``.
    """
    if not isinstance(part, Mapping):
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    attrs = attributes or {}
    labels: dict[str, Any] = {}
    raw_labels = attrs.get(OTEL_ATTR_LABELS)
    if isinstance(raw_labels, str) and raw_labels:
        try:
            decoded = json.loads(raw_labels)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            labels = decoded
    elif isinstance(raw_labels, Mapping):
        labels = dict(raw_labels)

    part_type = part.get(OTEL_TYPE)
    data: bytes | None = None
    uri: str | None = None
    if part_type == OTEL_TYPE_BLOB:
        data = decode_base64(part.get(OTEL_CONTENT), max_bytes=max_bytes)
    elif part_type == OTEL_TYPE_URI:
        uri = part.get(OTEL_URI)
        if not isinstance(uri, str) or not uri:
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
    elif part_type == OTEL_TYPE_FILE:
        file_id = part.get(OTEL_FILE_ID)
        if not isinstance(file_id, str) or not file_id:
            raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)
        labels = {**labels, LABEL_PROVIDER_FILE_ID: file_id}
    else:
        raise ValueError(ERROR_BLOCK_TYPE_UNSUPPORTED)

    content_type = part.get(OTEL_MIME_TYPE)
    resolved_kind = kind or attrs.get(OTEL_ATTR_KIND)
    if not resolved_kind and not content_type:
        resolved_kind = {
            MODALITY_IMAGE: KIND_SCREENSHOT,
            MODALITY_VIDEO: KIND_RECORDING,
            MODALITY_AUDIO: KIND_AUDIO,
        }.get(part.get(OTEL_MODALITY))
    return make_payload(
        kind=resolved_kind,
        name=attrs.get(OTEL_ATTR_NAME),
        content_type=content_type,
        data=data,
        uri=uri,
        labels=labels,
        sha256=attrs.get(OTEL_ATTR_SHA256),
    )

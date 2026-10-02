"""Write runtime session content into the chunked search corpus.

Every writer here is called from a path that already persists the source row,
runs inside a savepoint and never raises: an indexing failure must not fail
the call that produced the content, which is how the shipped gateway auto
indexing call site already behaves.

Chunking is deliberately boring and deterministic. Text is normalised, then
cut into ``CHUNK_SIZE_CHARS`` windows that advance by
``CHUNK_SIZE_CHARS - CHUNK_OVERLAP_CHARS`` characters, with the cut pulled
back to the last line or word boundary in the tail of the window when there is
one. The same input always produces the same chunks, so re-indexing a source
whose content and filter metadata did not change hashes identically and
writes nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_session_search_document
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.session_search_document import (
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_METADATA_ONLY,
    REDACTION_STATE_REDACTED,
    SOURCE_KIND_ARTIFACT,
    SOURCE_KIND_BROWSER_STEP,
    SOURCE_KIND_FLOW_LOG,
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_OPERATOR_NOTE,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TOOL_CALL,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
    SessionSearchDocument,
)
from preloop.services import artifact_text
from preloop.services.gateway_usage_search import GatewayUsageSearchService
from preloop.services.model_content_detectors import PII_EMAIL_RE
from preloop.utils.secret_scrubbing import REDACTED as SCRUB_PLACEHOLDER
from preloop.utils.secret_scrubbing import scrub_secrets

logger = logging.getLogger(__name__)

#: Chunk size and overlap, in characters, applied to every source kind.
#: 1200 characters is roughly a screen of transcript and comfortably below the
#: 1 MB ``tsvector`` limit even for dense text; the 200 character overlap keeps
#: a phrase that straddles a cut findable in at least one chunk.
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200
#: A single source never produces more than this many chunks. A runaway
#: payload costs a bounded number of rows, and the cut is deterministic.
MAX_CHUNKS_PER_SOURCE = 64
#: Chunk ceiling for one artifact: enough for the 1 MiB of extracted text
#: :mod:`preloop.services.artifact_text` keeps, at one chunk per
#: ``CHUNK_SIZE_CHARS - CHUNK_OVERLAP_CHARS`` characters, plus headroom for
#: the cut being pulled back to a word boundary.
ARTIFACT_MAX_CHUNKS = 1200
#: Mask applied to a credential-looking value found in free text.
REDACTED_VALUE = GatewayUsageSearchService.REDACTED_VALUE

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b([\w.-]{0,64}(?:api[_-]?key|authorization|secret|token|password)"
    r"[\w.-]{0,64})\s*[:=]\s*"
    r"(\"[^\"]{0,4096}+[^\"]*\"|'[^']{0,4096}+[^']*'|\S{1,4096}\S*)"
)
#: PEM private-key blocks pasted into transcript, notes, or tool summaries.
_PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----"
    r"(?:[A-Za-z0-9+/=\s]{1,16384})"
    r"-----END [A-Z0-9 ]{0,64}PRIVATE KEY-----"
)


@dataclass(frozen=True)
class RedactionOutcome:
    """What :func:`redact_indexed_source` did, and to how many chunks.

    ``action`` is one of ``reindexed``, ``dropped``, ``withheld`` or ``noop``.
    Callers audit it; the tests assert on it.
    """

    action: str
    chunks: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def indexing_enabled() -> bool:
    """Return whether the corpus accepts writes at all.

    The kill switch is checked on every write rather than cached, so flipping
    ``SESSION_SEARCH_INDEX_ENABLED`` stops indexing without a restart of a
    long lived worker.
    """
    return bool(getattr(settings, "session_search_index_enabled", True))


def chunk_text(text: str, *, max_chunks: int = MAX_CHUNKS_PER_SOURCE) -> List[str]:
    """Split normalised text into deterministic overlapping chunks."""
    return [chunk for _start, chunk in chunk_spans(text, max_chunks=max_chunks)]


def chunk_spans(
    text: str, *, max_chunks: int = MAX_CHUNKS_PER_SOURCE
) -> List[Tuple[int, str]]:
    """Chunks as :func:`chunk_text` cuts them, each with its start offset.

    The offset is into the stripped text, which is what lets a writer map a
    chunk back to the transcript cue it begins in.
    """
    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= CHUNK_SIZE_CHARS:
        return [(0, normalized)]

    chunks: List[Tuple[int, str]] = []
    start = 0
    length = len(normalized)
    while start < length and len(chunks) < max_chunks:
        end = min(start + CHUNK_SIZE_CHARS, length)
        window = normalized[start:end]
        if end < length:
            # Pull the cut back to the last line or word boundary in the tail
            # of the window, so a chunk ends where a reader would end it.
            tail_start = max(len(window) - CHUNK_OVERLAP_CHARS, 0)
            boundary = max(
                window.rfind("\n", tail_start),
                window.rfind(" ", tail_start),
            )
            if boundary > 0:
                end = start + boundary
                window = normalized[start:end]
        chunk = window.strip()
        if chunk:
            leading = len(window) - len(window.lstrip())
            chunks.append((start + leading, chunk))
        if end >= length:
            break
        start = max(end - CHUNK_OVERLAP_CHARS, start + 1)
    return chunks


def redact_text(text: str) -> tuple[str, bool]:
    """Mask credential-looking values in free text.

    Returns the text and whether anything was masked. Gateway payloads are
    sanitised by :class:`GatewayUsageSearchService` before they get here and
    are not touched again; this covers the free text sources (transcript
    messages, tool call summaries, operator notes) that never pass through a
    payload sanitiser.

    Labelled pairs (``api_key: value`` / ``secret=value``) are masked first.
    The value alternatives consume the rest of a matching run
    (``\\S{1,4096}\\S*``, and the quoted forms
    ``"[^"]{0,4096}+[^"]*"`` / ``'[^']{0,4096}+[^']*'``) so a value
    longer than 4096 characters is fully masked, not truncated. The
    bounded quoted parts are possessive so an unclosed quote cannot
    re-scan the run once per split point. Nothing follows those
    alternatives in the pattern, so per-start cost stays bounded by
    the first quantifier.

    A labelled key whose prefix before the keyword exceeds 64 characters
    does not match: there is no word boundary before the keyword inside a
    longer ``[\\w.-]`` run, so a 70-character prefix plus ``api_key=...``
    is not labelled-redacted. Provider-shaped values in that leftover are
    still scrubbed by :func:`scrub_secrets`.

    Known provider key prefixes, URL userinfo, query-parameter secrets and
    auth headers are masked next via :func:`scrub_secrets`. PEM private key
    blocks are masked last. Generic high-entropy blobs with no known prefix
    stay unmasked; that remaining gap is accepted until a broader scanner
    lands with the retention/legal-hold work.
    """
    if not text:
        return "", False
    redacted, _labelled_count = _CREDENTIAL_PATTERN.subn(
        lambda match: f"{match.group(1)}: {REDACTED_VALUE}", text
    )
    shaped = scrub_secrets(redacted) or ""
    if SCRUB_PLACEHOLDER != REDACTED_VALUE:
        shaped = shaped.replace(SCRUB_PLACEHOLDER, REDACTED_VALUE)
    pem, _pem_count = _PEM_PRIVATE_KEY_PATTERN.subn(REDACTED_VALUE, shaped)
    return pem, pem != text


def _resolve_redaction_state(*, captured: bool, redacted: bool) -> str:
    if not captured:
        return REDACTION_STATE_METADATA_ONLY
    return REDACTION_STATE_REDACTED if redacted else REDACTION_STATE_CLEAR


def _build_chunks(
    *,
    text: str,
    redaction_state: str,
    role: Optional[str],
    meta_data: Optional[Dict[str, Any]],
    model_alias: Optional[str] = None,
    provider_name: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    api_key_id: Optional[Any] = None,
    flow_id: Optional[Any] = None,
    status: Optional[str] = None,
    max_chunks: int = MAX_CHUNKS_PER_SOURCE,
    chunk_meta: Optional[Callable[[int], Dict[str, Any]]] = None,
) -> List[SessionSearchChunk]:
    spans = chunk_spans(text, max_chunks=max_chunks)
    total = len(spans)
    return [
        SessionSearchChunk(
            content=piece,
            chunk_index=index,
            role=role,
            redaction_state=redaction_state,
            embedding_state=EMBEDDING_STATE_PENDING,
            model_alias=model_alias,
            provider_name=provider_name,
            runtime_principal_id=runtime_principal_id,
            api_key_id=api_key_id,
            flow_id=flow_id,
            status=status,
            meta_data={
                **(meta_data or {}),
                **(chunk_meta(offset) if chunk_meta is not None else {}),
                "chunk_count": total,
            },
        )
        for index, (offset, piece) in enumerate(spans)
    ]


def request_embedding(account_id: Any) -> None:
    """Nudge the embedding worker after the host transaction has committed.

    ``write_source_chunks`` calls this only on ``commit=True``. Callers that
    write chunks inside someone else's transaction (transcript import) must
    invoke this themselves after they commit, so imported chunks do not wait
    for an unrelated later write. The submission is still deduplicated and
    dropped when the queue is full; embedding runs on the worker's own
    session.
    """
    try:
        from preloop.services.session_embedding_queue import (
            submit_account_for_embedding,
        )

        submit_account_for_embedding(account_id)
    except Exception:  # noqa: BLE001 - indexing never fails over embedding
        logger.warning(
            "Session embedding hand-off failed for account %s",
            account_id,
            exc_info=True,
        )


def _request_embedding(account_id: Any, stored: List[SessionSearchDocument]) -> None:
    """Nudge after this writer committed pending chunks.

    Callers must invoke this only after the host transaction has committed.
    A nudge while the writer's transaction is still open wakes the worker
    on rows it cannot see, burns a submission, and leaves the chunks
    waiting for a later write.
    """
    if not stored:
        return
    if not any(row.embedding_state == EMBEDDING_STATE_PENDING for row in stored):
        return
    request_embedding(account_id)


def write_source_chunks(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_kind: str,
    source_id: Any,
    text: str,
    occurred_at: Optional[datetime] = None,
    role: Optional[str] = None,
    content_captured: bool = True,
    already_sanitised: bool = False,
    meta_data: Optional[Dict[str, Any]] = None,
    model_alias: Optional[str] = None,
    provider_name: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    api_key_id: Optional[Any] = None,
    flow_id: Optional[Any] = None,
    status: Optional[str] = None,
    commit: bool = False,
    existing: Optional[Sequence[SessionSearchDocument]] = None,
    max_chunks: int = MAX_CHUNKS_PER_SOURCE,
    chunk_meta: Optional[Callable[[int], Dict[str, Any]]] = None,
) -> List[SessionSearchDocument]:
    """Write one source's chunks, swallowing every failure.

    The write runs inside a savepoint: a failure rolls back the chunk rows
    only and leaves the caller's transaction usable, which is the difference
    between a missing search hit and a lost gateway call.
    """
    if not indexing_enabled():
        return []
    if account_id is None or runtime_session_id is None or source_id is None:
        return []

    try:
        redacted = False
        if content_captured and not already_sanitised:
            text, redacted = redact_text(text)
        elif content_captured:
            redacted = REDACTED_VALUE in (text or "")
        redaction_state = _resolve_redaction_state(
            captured=content_captured, redacted=redacted
        )
        chunks = _build_chunks(
            text=text,
            redaction_state=redaction_state,
            role=role,
            meta_data={
                **(meta_data or {}),
                "content_captured": bool(content_captured),
            },
            model_alias=model_alias,
            provider_name=provider_name,
            runtime_principal_id=runtime_principal_id,
            api_key_id=api_key_id,
            flow_id=flow_id,
            status=status,
            max_chunks=max_chunks,
            chunk_meta=chunk_meta,
        )
        if not chunks:
            return []

        savepoint = db.begin_nested()
        try:
            stored = crud_session_search_document.replace_source_chunks(
                db,
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                source_kind=source_kind,
                source_id=str(source_id),
                occurred_at=occurred_at or _now(),
                chunks=chunks,
                existing=existing,
            )
        except Exception:
            if savepoint.is_active:
                savepoint.rollback()
            raise
        else:
            if savepoint.is_active:
                savepoint.commit()
        if commit:
            # A failed Session.commit leaves the shared session in a
            # pending-rollback state. Recover it before the outer swallow
            # so the caller's next query is not a PendingRollbackError.
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
            # Production callers discard the returned list. Refreshing here
            # would be one extra SELECT per chunk on the gateway path for
            # attributes nobody reads. The embedding nudge runs after
            # commit and only inspects embedding_state.
            _request_embedding(account_id, stored)
        return stored
    except Exception:  # noqa: BLE001 - indexing never fails its caller
        logger.warning(
            "Session search indexing failed for %s %s",
            source_kind,
            source_id,
            exc_info=True,
        )
        return []


def index_gateway_interaction(
    db: Session,
    *,
    usage: ApiUsage,
    request_payload: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one metered gateway interaction as session chunks.

    The text is built by the shipped gateway search service, sanitiser
    included and unchanged, so the corpus and the gateway document agree on
    what a captured interaction looks like.
    """
    if not indexing_enabled():
        return []
    try:
        runtime_session_id = getattr(usage, "runtime_session_id", None)
        if runtime_session_id is None:
            return []
        service = GatewayUsageSearchService(db)
        content_captured = bool(settings.model_gateway_capture_content)
        prepared_request = (
            service.payload_for_indexing(request_payload) if content_captured else None
        )
        prepared_response = (
            service.payload_for_indexing(response_payload) if content_captured else None
        )
        text = service.build_searchable_text(
            usage=usage,
            request_payload=prepared_request,
            response_payload=prepared_response,
        )
        meta_data = service.build_document_metadata(
            usage=usage,
            request_payload=prepared_request,
            response_payload=prepared_response,
        )
    except Exception:  # noqa: BLE001 - indexing never fails its caller
        logger.warning(
            "Session search text build failed for gateway usage %s",
            getattr(usage, "id", None),
            exc_info=True,
        )
        return []

    return write_source_chunks(
        db,
        account_id=usage.account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=usage.id,
        text=text,
        occurred_at=usage.timestamp,
        role="assistant",
        content_captured=content_captured,
        already_sanitised=True,
        meta_data=meta_data,
        model_alias=usage.model_alias,
        provider_name=usage.provider_name,
        runtime_principal_id=usage.runtime_principal_id,
        api_key_id=usage.api_key_id,
        flow_id=usage.flow_id,
        status=str(usage.status_code) if usage.status_code is not None else None,
        commit=commit,
    )


def index_transcript_message(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    text: str,
    role: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one pushed transcript message."""
    content_captured = bool(settings.model_gateway_capture_content)
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        text=text
        if content_captured
        else _descriptor(kind=SOURCE_KIND_TRANSCRIPT_MESSAGE, role=role, text=text),
        occurred_at=occurred_at,
        role=role,
        content_captured=content_captured,
        meta_data=meta_data,
        status=role,
        commit=commit,
    )


def index_tool_call(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    server_name: Optional[str],
    tool_name: Optional[str],
    status: Optional[str],
    summary: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    api_key_id: Optional[Any] = None,
    flow_id: Optional[Any] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
    existing: Optional[Sequence[SessionSearchDocument]] = None,
) -> List[SessionSearchDocument]:
    """Index one tool call activity."""
    content_captured = bool(settings.model_gateway_capture_content)
    header = "\n".join(
        line
        for line in (
            f"kind: {SOURCE_KIND_TOOL_CALL}",
            f"server_name: {server_name}" if server_name else "",
            f"tool_name: {tool_name}" if tool_name else "",
            f"status: {status}" if status else "",
        )
        if line
    )
    text = f"{header}\n{summary}" if (summary and content_captured) else header
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_TOOL_CALL,
        source_id=source_id,
        text=text,
        occurred_at=occurred_at,
        role="tool",
        content_captured=content_captured,
        meta_data=meta_data,
        api_key_id=api_key_id,
        flow_id=flow_id,
        status=status,
        commit=commit,
        existing=existing,
    )


def index_browser_step(
    db: Session,
    *,
    activity: Any,
    commit: bool = True,
) -> List[SessionSearchDocument]:
    """Index one browser step activity.

    The searchable text is the action, URL, target and reasoning, masked
    with :func:`redact_text`. Content capture gates the body the same way
    tool calls do: with capture off the chunk is a descriptor and the
    reasoning is not stored.

    Args:
        db: Database session.
        activity: Stored ``browser_step`` activity row.
        commit: Whether to commit the chunk write. Callers that already
            own a transaction pass ``False`` and commit once.

    Returns:
        The chunks now stored for this step. Empty when indexing is
        disabled or the write fails; a failure is logged and never raised.
    """
    metadata = getattr(activity, "metadata_", None) or {}
    action = str(metadata.get("action") or getattr(activity, "tool_name", None) or "")
    url = str(metadata.get("url") or "")
    target = str(metadata.get("target") or "")
    reasoning = str(metadata.get("reasoning") or "")
    body = f"{action} {url} {target} {reasoning}".strip()
    content_captured = bool(settings.model_gateway_capture_content)
    if content_captured:
        body = redact_text(body)[0]
        text = f"kind: {SOURCE_KIND_BROWSER_STEP}\n{body}"
    else:
        text = _descriptor(kind=SOURCE_KIND_BROWSER_STEP, role="browser", text=body)
    if getattr(activity, "id", None) is None:
        db.flush()
    return write_source_chunks(
        db,
        account_id=activity.account_id,
        runtime_session_id=activity.runtime_session_id,
        source_kind=SOURCE_KIND_BROWSER_STEP,
        source_id=activity.id,
        text=text,
        occurred_at=getattr(activity, "timestamp", None),
        role="browser",
        content_captured=content_captured,
        already_sanitised=True,
        meta_data={"activity_type": "browser_step"},
        api_key_id=getattr(activity, "api_key_id", None),
        status=getattr(activity, "status", None),
        commit=commit,
    )


def redact_artifact_text(text: str) -> tuple[str, bool]:
    """:func:`redact_text`, plus email addresses masked.

    Artifact text is the one source that is a whole document a person wrote
    or said, so the address of whoever is named in it is masked as well.
    Phone numbers and names are not: the shipped phone pattern is US shaped
    and would leave a partial number behind. A pluggable redaction hook is
    #1100.
    """
    masked, changed = redact_text(text)
    masked, emails = PII_EMAIL_RE.subn(REDACTED_VALUE, masked)
    return masked, bool(changed or emails)


def _artifact_header(artifact: Any) -> str:
    labels = getattr(artifact, "labels", None) or {}
    label_text = " ".join(
        f"{key}={' '.join(map(str, value)) if isinstance(value, list) else value}"
        for key, value in sorted(labels.items())
    )
    lines = [
        f"kind: {SOURCE_KIND_ARTIFACT}",
        f"artifact_kind: {artifact.kind}",
        f"name: {artifact.name}" if getattr(artifact, "name", None) else "",
        f"tool_name: {artifact.tool_name}"
        if getattr(artifact, "tool_name", None)
        else "",
        f"labels: {label_text}" if label_text else "",
    ]
    return redact_artifact_text("\n".join(line for line in lines if line))[0]


def _artifact_plaintext(artifact: Any) -> Optional[bytes]:
    from preloop.models.crud import runtime_session_artifact as crud_artifact

    if getattr(artifact, "ciphertext", None) is None:
        return None
    try:
        return crud_artifact.decrypt(artifact)
    except ValueError:
        logger.warning("Artifact %s could not be decrypted for indexing", artifact.id)
        return None


def _record_text_status(artifact: Any, extracted: Any) -> None:
    has_text = extracted is not None and not extracted.empty
    artifact.text_status = (
        artifact_text.TEXT_STATUS_EXTRACTED
        if has_text
        else artifact_text.TEXT_STATUS_NONE
    )
    if has_text and extracted.truncated:
        manifest = dict(artifact.manifest or {})
        manifest["text_truncated"] = True
        artifact.manifest = manifest


def index_artifact_text(
    db: Session,
    artifact: Any,
    *,
    commit: bool = True,
) -> List[SessionSearchDocument]:
    """Index one stored session artifact into the session corpus.

    Transcripts and text documents are extracted by
    :mod:`preloop.services.artifact_text`, masked with
    :func:`redact_artifact_text`, chunked and written with source kind
    ``artifact``. Every chunk carries the artifact id, its timeline
    ``activity_id``, kind, name and labels in ``meta_data``; a timed
    transcript chunk also carries ``cue_start``, the start in seconds of the
    cue the chunk begins in. Other kinds (images, audio, PDF) index the
    name, labels and tool only.

    Records ``text_status`` (and ``manifest.text_truncated`` past the 1 MiB
    text cap) on the artifact. Content capture gates the body the same way
    it does for every other source. Never raises.
    """
    try:
        extracted = None
        if artifact_text.supports(artifact.kind, artifact.content_type):
            data = _artifact_plaintext(artifact)
            if data is not None:
                extracted = artifact_text.extract_text(
                    artifact.kind, artifact.content_type, data
                )
        _record_text_status(artifact, extracted)
        # Commit the status on its own: the chunk write below returns without
        # committing when indexing is off or it fails, and the request session
        # is closed with a rollback.
        if commit:
            db.commit()
        else:
            db.flush()
    except Exception:  # noqa: BLE001 - indexing never fails its caller
        logger.warning(
            "Artifact text extraction failed for %s", artifact.id, exc_info=True
        )
        db.rollback()
        return []

    header = _artifact_header(artifact)
    content_captured = bool(settings.model_gateway_capture_content)
    has_text = extracted is not None and not extracted.empty
    cue_offsets: List[Tuple[int, Optional[float]]] = []
    if has_text and content_captured:
        if extracted.cues:
            lines: List[str] = []
            position = len(header) + 1
            for cue in extracted.cues:
                line = redact_artifact_text(cue.text)[0]
                cue_offsets.append((position, cue.start))
                lines.append(line)
                position += len(line) + 1
            body = "\n".join(lines)
        else:
            body = redact_artifact_text(extracted.text)[0]
        text = f"{header}\n{body}"
    elif has_text:
        plain = extracted.text or "\n".join(cue.text for cue in extracted.cues)
        text = f"{header}\n" + _descriptor(
            kind=SOURCE_KIND_ARTIFACT, role="artifact", text=plain
        )
    else:
        text = header

    def chunk_meta(offset: int) -> Dict[str, Any]:
        if not cue_offsets:
            return {}
        start = cue_offsets[0][1]
        for position, cue_start in cue_offsets:
            if position > offset:
                break
            start = cue_start
        return {"cue_start": start} if start is not None else {}

    meta_data: Dict[str, Any] = {
        "artifact_id": str(artifact.id),
        "activity_id": str(artifact.activity_id) if artifact.activity_id else None,
        "kind": artifact.kind,
        "name": artifact.name,
        "labels": dict(artifact.labels or {}),
        "content_type": artifact.content_type,
        "tool_name": artifact.tool_name,
        "text_status": artifact.text_status,
    }
    if has_text and extracted.truncated:
        meta_data["text_truncated"] = True
    return write_source_chunks(
        db,
        account_id=artifact.account_id,
        runtime_session_id=artifact.runtime_session_id,
        source_kind=SOURCE_KIND_ARTIFACT,
        source_id=artifact.id,
        text=text,
        occurred_at=getattr(artifact, "created_at", None),
        role="artifact",
        content_captured=content_captured,
        already_sanitised=True,
        meta_data=meta_data,
        status=artifact.kind,
        commit=commit,
        max_chunks=ARTIFACT_MAX_CHUNKS,
        chunk_meta=chunk_meta,
    )


def index_operator_note(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    body: Optional[str],
    status: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one operator note as it reaches a session."""
    content_captured = bool(settings.model_gateway_capture_content)
    text = (
        f"kind: {SOURCE_KIND_OPERATOR_NOTE}\n{body}"
        if (body and content_captured)
        else _descriptor(
            kind=SOURCE_KIND_OPERATOR_NOTE, role="operator", text=body or ""
        )
    )
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_OPERATOR_NOTE,
        source_id=source_id,
        text=text,
        occurred_at=occurred_at,
        role="operator",
        content_captured=content_captured,
        meta_data=meta_data,
        status=status,
        commit=commit,
    )


def _drop_session_summary_chunks(
    db: Session, *, runtime_session_id: Any, commit: bool
) -> int:
    """Remove a session's summary chunks, swallowing every failure.

    Same discipline as :func:`write_source_chunks`: a savepoint so a failed
    delete leaves the caller's transaction usable, and a warning instead of
    an exception so a title write is never lost over the corpus.
    """
    if runtime_session_id is None:
        return 0
    try:
        savepoint = db.begin_nested()
        try:
            removed = crud_session_search_document.delete_for_source(
                db,
                source_kind=SOURCE_KIND_SESSION_SUMMARY,
                source_id=str(runtime_session_id),
            )
        except Exception:
            if savepoint.is_active:
                savepoint.rollback()
            raise
        else:
            if savepoint.is_active:
                savepoint.commit()
        if removed and commit:
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise
        return removed
    except Exception:  # noqa: BLE001 - indexing never fails its caller
        logger.warning(
            "Session summary chunk cleanup failed for session %s",
            runtime_session_id,
            exc_info=True,
        )
        return 0


def index_session_summary(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    title: Optional[str],
    summary: Optional[str],
    occurred_at: Optional[datetime] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index a session's own title and summary.

    A session that has neither a title nor a summary has nothing that
    describes it, so any chunk left by an earlier title is deleted instead of
    being replaced by a header with no content. That is what keeps a cleared
    summary, or a title write that never produced a title, from answering a
    search with text the session no longer carries.

    Content capture gates the summary line. A session with no title and a
    summary that capture forbids would otherwise store only the kind header,
    so that case is treated as empty and the stale chunk is dropped. The
    title line is metadata and still writes when capture is off.

    Cleanup still runs when the indexing kill switch is on. Writes do not.
    A cleared session must not keep answering searches with text it no
    longer carries, which is the same reason redaction deletes are not
    gated on the switch.
    """
    title_text = (title or "").strip()
    summary_text = (summary or "").strip()
    content_captured = bool(settings.model_gateway_capture_content)
    if not title_text and not (summary_text and content_captured):
        _drop_session_summary_chunks(
            db, runtime_session_id=runtime_session_id, commit=commit
        )
        return []
    if not indexing_enabled():
        return []
    body = "\n".join(
        line
        for line in (
            f"kind: {SOURCE_KIND_SESSION_SUMMARY}",
            f"title: {title_text}" if title_text else "",
            f"summary: {summary_text}" if (summary_text and content_captured) else "",
        )
        if line
    )
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=runtime_session_id,
        text=body,
        occurred_at=occurred_at,
        role="system",
        content_captured=content_captured,
        meta_data=meta_data,
        commit=commit,
    )


def index_flow_log(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    text: str,
    occurred_at: Optional[datetime] = None,
    flow_id: Optional[Any] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one flow log excerpt attached to a session.

    No call site yet. Flow logs are the one source whose volume needs a
    sampling rule of its own before anything indexes it wholesale, so the
    kind and its writer land here and the decision lands with the caller.
    """
    content_captured = bool(settings.model_gateway_capture_content)
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_FLOW_LOG,
        source_id=source_id,
        text=text
        if content_captured
        else _descriptor(kind=SOURCE_KIND_FLOW_LOG, role="system", text=text),
        occurred_at=occurred_at,
        role="system",
        content_captured=content_captured,
        flow_id=flow_id,
        meta_data=meta_data,
        commit=commit,
    )


def redact_indexed_source(
    db: Session,
    *,
    source_kind: str,
    source_id: Any,
    replacement_text: Optional[str] = None,
    account_id: Optional[Any] = None,
    runtime_session_id: Optional[Any] = None,
    occurred_at: Optional[datetime] = None,
    role: Optional[str] = None,
    drop: bool = False,
    commit: bool = False,
) -> RedactionOutcome:
    """Apply a redaction that happened after the source was indexed.

    Indexing at write time sanitises what it stores, but a source can be
    redacted later: content capture is turned off, a payload is scrubbed, an
    operator removes a note. The corpus is a second copy of that content, and
    a second copy that keeps answering with the old text is the whole reason a
    search index is a compliance problem rather than a feature.

    Three outcomes, in the order a caller should prefer them:

    ``reindexed``
        ``replacement_text`` was given, so the source is written again through
        the normal write path and the chunks now hold the redacted text. The
        session, account and timestamp are read off the existing chunks when
        the caller does not pass them, so a redactor only needs the source it
        is redacting.
    ``dropped``
        ``drop=True``, or the replacement is empty: the chunks go. The right
        answer when the source itself is gone.
    ``withheld``
        Neither: the stored text is cleared in place and the chunks are marked
        :data:`~preloop.models.models.session_search_document.REDACTION_STATE_WITHHELD`.
        The rows remain so the search still knows something happened, and the
        read path returns no text for them.

    Unlike the writers this does not swallow its exceptions. A redaction that
    quietly failed would leave the operator believing text was removed when it
    was not, which is worse than an error.
    """
    existing = crud_session_search_document.list_for_source(
        db, source_kind=source_kind, source_id=str(source_id)
    )
    if not existing:
        return RedactionOutcome(action="noop", chunks=0)

    if drop or (replacement_text is not None and not replacement_text.strip()):
        removed = crud_session_search_document.delete_for_source(
            db, source_kind=source_kind, source_id=str(source_id)
        )
        if commit:
            db.commit()
        return RedactionOutcome(action="dropped", chunks=removed)

    if replacement_text is not None:
        first = existing[0]
        stored = write_source_chunks(
            db,
            account_id=account_id if account_id is not None else first.account_id,
            runtime_session_id=(
                runtime_session_id
                if runtime_session_id is not None
                else first.runtime_session_id
            ),
            source_kind=source_kind,
            source_id=source_id,
            text=replacement_text,
            occurred_at=occurred_at or first.occurred_at,
            role=role if role is not None else first.role,
            meta_data=_carried_meta(source_kind, first.meta_data),
            model_alias=first.model_alias,
            provider_name=first.provider_name,
            runtime_principal_id=first.runtime_principal_id,
            api_key_id=first.api_key_id,
            flow_id=first.flow_id,
            status=first.status,
            commit=commit,
        )
        if not stored:
            # The write path swallows its own failures and the corpus must not
            # be left holding the pre-redaction text because of one.
            removed = crud_session_search_document.delete_for_source(
                db, source_kind=source_kind, source_id=str(source_id)
            )
            if commit:
                db.commit()
            return RedactionOutcome(action="dropped", chunks=removed)
        return RedactionOutcome(action="reindexed", chunks=len(stored))

    marked = crud_session_search_document.withhold_source_text(
        db, source_kind=source_kind, source_id=str(source_id)
    )
    if commit:
        db.commit()
    return RedactionOutcome(action="withheld", chunks=marked)


def _carried_meta(
    source_kind: str, previous: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Metadata a redaction rewrite keeps from the chunks it replaces.

    An artifact chunk is found and filtered by the identity in its metadata
    (artifact id, kind, labels), so a redaction must not strip it. Per-chunk
    keys do not carry over: the replacement text is chunked afresh.
    """
    carried: Dict[str, Any] = {}
    if source_kind == SOURCE_KIND_ARTIFACT and previous:
        carried = {
            key: value
            for key, value in previous.items()
            if key not in ("chunk_count", "cue_start", "content_captured")
        }
    carried["redacted_after_indexing"] = True
    return carried


def _descriptor(*, kind: str, role: Optional[str], text: str) -> str:
    """Return the metadata only stand in for content we may not store.

    A chunk written with capture disabled says what existed and how big it
    was. That keeps the row honest (and countable) instead of writing an
    empty chunk that looks like content nobody wrote.
    """
    lines = [f"kind: {kind}", "content_captured: false"]
    if role:
        lines.append(f"role: {role}")
    lines.append(f"content_length: {len(text or '')}")
    return "\n".join(lines)

"""Optional product/release/build mapping for CRA product-mode audits.

The agent may echo checkout SHAs it observed. Those notes are never treated as
cryptographic build attestation. A mapping is verified only against trusted
checkout/runtime facts and supplied artifact bytes.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from preloop.utils.workspace_seed import (
    WorkspaceSeedError,
    WorkspaceSeedFile,
    parse_workspace_files,
)

PRODUCT_PROVENANCE_SCHEMA = "preloop.cra.product_provenance/v1"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
_CLONE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ROLES = frozenset({"code", "compliance"})


class ProductProvenanceError(ValueError):
    """A recoverable, safe-to-display product mapping failure."""


class AmbiguousProductMappingError(ProductProvenanceError):
    """The mapping does not uniquely identify each repository or role."""


class DuplicateProductMappingError(ProductProvenanceError):
    """The mapping repeats a remote, clone path, or SHA assignment."""


class MismatchedProductMappingError(ProductProvenanceError):
    """The mapping disagrees with trusted checkout or artifact facts."""


class UnauthorizedProductMappingError(ProductProvenanceError):
    """The mapping names a repository outside the authorized account set."""


@dataclass(frozen=True)
class ProvenanceRepository:
    """One constituent repository in a product mapping."""

    remote: str
    sha: str
    clone_path: str
    role: str
    sha_status: str


@dataclass(frozen=True)
class ProductIdentity:
    """Supported-release and build identity supplied with a mapping."""

    product_name: str
    product_id: str | None
    release_identifier: str
    release_channel: str | None
    build_id: str | None
    build_url: str | None


@dataclass(frozen=True)
class VerifiedProductProvenance:
    """Control-plane record of a mapping after validation.

    ``sha_status`` is ``verified`` only when the SHA matched a trusted fact.
    Agent-written SHAs never produce ``verified``.
    """

    schema: str
    identity: ProductIdentity
    sbom_digest: str | None
    sbom_path: str | None
    sbom_status: str
    repositories: tuple[ProvenanceRepository, ...]
    mapping_status: str
    unverified_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready record for execution result (control-plane owned)."""
        return {
            "schema": self.schema,
            "mapping_status": self.mapping_status,
            "product": {
                "name": self.identity.product_name,
                "id": self.identity.product_id,
            },
            "release": {
                "identifier": self.identity.release_identifier,
                "channel": self.identity.release_channel,
            },
            "build": {
                "id": self.identity.build_id,
                "url": self.identity.build_url,
            },
            "sbom": {
                "digest": self.sbom_digest,
                "path": self.sbom_path,
                "status": self.sbom_status,
            },
            "repositories": [
                {
                    "remote": repo.remote,
                    "sha": repo.sha,
                    "clone_path": repo.clone_path,
                    "role": repo.role,
                    "sha_status": repo.sha_status,
                }
                for repo in self.repositories
            ],
            "unverified_reasons": list(self.unverified_reasons),
            "attestation": (
                "verified means the SHA was present in a frozen checkout "
                "bundle. pin_matched means it equalled a controller-resolved "
                "commit that has not been observed in a bundle. Agent-written "
                "SHAs are declarations, not a cryptographic build attestation."
            ),
        }


@dataclass(frozen=True)
class RuntimeProvenanceFacts:
    """Trusted facts observed by the control plane, never by the agent.

    ``clone_shas`` are observed checkout identities (frozen bundle contains
    the pin). ``requested_pins`` are controller-resolved commits that have
    not been observed yet and must not be labelled ``verified``.
    """

    authorized_remotes: tuple[str, ...]
    clone_paths: tuple[str, ...]
    clone_shas: Mapping[str, str]
    sbom_bytes: bytes | None
    sbom_path: str | None
    requested_pins: Mapping[str, str] = field(default_factory=dict)
    publication_approval: Mapping[str, Any] | None = None


def normalize_repository_url(value: str) -> str:
    """Canonical credential-free HTTPS git remote (no userinfo/query/fragment)."""
    if not isinstance(value, str) or not value.strip():
        raise ProductProvenanceError("Repository remote is required")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        raise ProductProvenanceError(
            "Product mapping remotes must be credential-free HTTPS URLs"
        )
    host = parsed.hostname.lower()
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+", path):
        raise ProductProvenanceError("Invalid repository path in product mapping")
    return f"https://{host}{path}.git"


def clone_path_slug(clone_path: str) -> str:
    """Stable archive/bundle name from a configured clone path."""
    if not isinstance(clone_path, str) or not clone_path.strip():
        raise ProductProvenanceError("clone_path is required")
    trimmed = clone_path.strip().rstrip("/")
    if trimmed in {"/workspace", "workspace", "."}:
        slug = "workspace"
    else:
        slug = trimmed.split("/")[-1]
    if not _CLONE_SLUG.fullmatch(slug):
        raise ProductProvenanceError("clone_path is not a safe repository slug")
    return slug


def sha256_digest(data: bytes) -> str:
    """Return a ``sha256:<hex>`` digest of exact bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def parse_sbom_digest(value: Any) -> str:
    """Require an explicit SHA-256 digest, never a truncated or git SHA."""
    if not isinstance(value, str) or not value.strip():
        raise ProductProvenanceError("SBOM digest is required in product mapping")
    raw = value.strip().lower()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if not _SHA256.fullmatch(raw):
        raise MismatchedProductMappingError(
            "SBOM digest must be sha256:<64 hex>; git SHAs are not SBOM digests"
        )
    return f"sha256:{raw}"


def extract_product_provenance_payload(
    trigger_event_data: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Read optional mapping from a trigger payload. Missing is legacy mode."""
    if not isinstance(trigger_event_data, Mapping):
        return None
    payload = trigger_event_data.get("payload")
    if not isinstance(payload, Mapping):
        payload = trigger_event_data
    mapping = payload.get("product_provenance")
    if mapping is None:
        return None
    if not isinstance(mapping, dict):
        raise ProductProvenanceError("product_provenance must be a JSON object")
    return mapping


def facts_from_workspace_files(
    trigger_event_data: Mapping[str, Any] | None,
    *,
    sbom_path: str | None,
) -> tuple[bytes | None, str | None]:
    """Hash a supplied workspace seed file when the mapping names one."""
    if not isinstance(trigger_event_data, Mapping):
        return None, None
    payload = trigger_event_data.get("payload")
    if not isinstance(payload, Mapping):
        payload = trigger_event_data
    try:
        files = parse_workspace_files(dict(payload) if payload else None)
    except WorkspaceSeedError:
        return None, None
    if not files:
        return None, None
    chosen: WorkspaceSeedFile | None = None
    if sbom_path:
        for item in files:
            if item.path == sbom_path:
                chosen = item
                break
        if chosen is None:
            raise MismatchedProductMappingError(
                "Mapped SBOM path was not present in supplied workspace files"
            )
    else:
        candidates = [
            item
            for item in files
            if item.path.endswith((".spdx.json", ".cdx.json", ".spdx", "bom.json"))
            or "sbom" in item.path.lower()
        ]
        if len(candidates) > 1:
            raise AmbiguousProductMappingError(
                "Multiple SBOM-like workspace files supplied; name sbom.path"
            )
        if len(candidates) == 1:
            chosen = candidates[0]
    if chosen is None:
        return None, None
    return base64.b64decode(chosen.content_base64, validate=True), chosen.path


def facts_from_git_clone_config(
    git_clone_config: Mapping[str, Any] | None,
    *,
    clone_shas: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]:
    """Authorized remotes and clone paths from the trusted flow config."""
    config = git_clone_config if isinstance(git_clone_config, Mapping) else {}
    repositories = config.get("repositories") or []
    remotes: list[str] = []
    paths: list[str] = []
    seen_remote: set[str] = set()
    seen_path: set[str] = set()
    for index, repo in enumerate(repositories):
        if not isinstance(repo, Mapping):
            raise ProductProvenanceError(
                "git_clone_config.repositories entries must be objects"
            )
        url = repo.get("repository_url")
        if not url:
            continue
        remote = normalize_repository_url(str(url))
        path = str(repo.get("clone_path") or f"workspace-{index + 1}")
        slug = clone_path_slug(path)
        if remote in seen_remote:
            raise DuplicateProductMappingError(
                "git_clone_config repeats the same repository remote"
            )
        if slug in seen_path:
            raise DuplicateProductMappingError(
                "git_clone_config repeats the same clone_path"
            )
        seen_remote.add(remote)
        seen_path.add(slug)
        remotes.append(remote)
        paths.append(slug)
    trusted_shas: dict[str, str] = {}
    for remote, sha in (clone_shas or {}).items():
        trusted_shas[normalize_repository_url(remote)] = _require_git_sha(sha)
    return tuple(remotes), tuple(paths), trusted_shas


def is_git_sha(value: Any) -> bool:
    """True when ``value`` is an exact 40-hex git object name."""
    return isinstance(value, str) and bool(_GIT_SHA.fullmatch(value.lower()))


def _require_git_sha(value: Any) -> str:
    if not isinstance(value, str) or not _GIT_SHA.fullmatch(value.lower()):
        raise MismatchedProductMappingError(
            "Repository SHA must be an exact 40-hex git object name"
        )
    return value.lower()


def _parse_identity(mapping: Mapping[str, Any]) -> ProductIdentity:
    product = mapping.get("product")
    if isinstance(product, str) and product.strip():
        name = product.strip()
        product_id = None
    elif isinstance(product, Mapping):
        name = str(product.get("name") or "").strip()
        raw_id = product.get("id")
        product_id = str(raw_id).strip() if raw_id else None
    else:
        name = ""
        product_id = None
    if not name:
        raise ProductProvenanceError("product.name is required in product mapping")
    release = mapping.get("release") or mapping.get("supported_release")
    if isinstance(release, str) and release.strip():
        identifier = release.strip()
        channel = None
    elif isinstance(release, Mapping):
        identifier = str(release.get("identifier") or release.get("id") or "").strip()
        raw_channel = release.get("channel")
        channel = str(raw_channel).strip() if raw_channel else None
    else:
        identifier = ""
        channel = None
    if not identifier or not _RELEASE_ID.fullmatch(identifier):
        raise ProductProvenanceError(
            "release.identifier is required and must be a stable supported-release id"
        )
    build = mapping.get("build")
    build_id = None
    build_url = None
    if isinstance(build, str) and build.strip():
        build_id = build.strip()
    elif isinstance(build, Mapping):
        raw_id = build.get("id") or build.get("identifier")
        build_id = str(raw_id).strip() if raw_id else None
        raw_url = build.get("url")
        if raw_url:
            parsed = urlsplit(str(raw_url))
            if (
                parsed.scheme not in {"https", "http"}
                or parsed.username
                or parsed.password
            ):
                raise ProductProvenanceError(
                    "build.url must be a credential-free http(s) URL when present"
                )
            build_url = str(raw_url)
    return ProductIdentity(
        product_name=name,
        product_id=product_id,
        release_identifier=identifier,
        release_channel=channel,
        build_id=build_id,
        build_url=build_url,
    )


def _parse_mapping_repositories(
    mapping: Mapping[str, Any],
) -> list[dict[str, str]]:
    rows = mapping.get("repositories")
    if not isinstance(rows, list) or not rows:
        raise ProductProvenanceError(
            "product mapping must list constituent repositories with exact SHAs"
        )
    parsed: list[dict[str, str]] = []
    seen_remote: set[str] = set()
    seen_path: set[str] = set()
    seen_pair: set[tuple[str, str]] = set()
    compliance = 0
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProductProvenanceError("repositories[] entries must be objects")
        remote = normalize_repository_url(
            str(row.get("remote") or row.get("repository_url") or "")
        )
        sha = _require_git_sha(
            row.get("sha") or row.get("commit") or row.get("head_sha")
        )
        path = clone_path_slug(str(row.get("clone_path") or row.get("path") or ""))
        role = str(row.get("role") or "code").strip().lower()
        if role not in _ROLES:
            raise ProductProvenanceError("repository role must be code or compliance")
        if remote in seen_remote:
            raise DuplicateProductMappingError(
                "product mapping lists the same remote more than once"
            )
        if path in seen_path:
            raise DuplicateProductMappingError(
                "product mapping lists the same clone_path more than once"
            )
        pair = (remote, sha)
        if pair in seen_pair:
            raise DuplicateProductMappingError(
                "product mapping repeats the same remote+SHA pair"
            )
        seen_remote.add(remote)
        seen_path.add(path)
        seen_pair.add(pair)
        if role == "compliance":
            compliance += 1
        parsed.append({"remote": remote, "sha": sha, "clone_path": path, "role": role})
    if compliance > 1:
        raise AmbiguousProductMappingError(
            "product mapping names more than one compliance repository"
        )
    return parsed


def validate_product_provenance(
    mapping: Mapping[str, Any] | None,
    facts: RuntimeProvenanceFacts,
    *,
    require_mapping: bool = False,
    require_observed: bool = False,
) -> VerifiedProductProvenance | None:
    """Validate an optional mapping against trusted facts.

    Args:
        mapping: Trigger payload mapping, or None for legacy flows.
        facts: Control-plane checkout URLs/SHAs and supplied SBOM bytes.
        require_mapping: When True (product-mode audits that opted in), a
            missing mapping fails rather than falling back to legacy.
        require_observed: When True, product-mode SHAs must match frozen
            checkout bundles. Controller-resolved pins alone are not
            ``verified``.

    Returns:
        A verified record, or None when the mapping is absent and not required.

    Raises:
        ProductProvenanceError: Invalid, duplicate, ambiguous, unauthorized,
            or mismatched mapping.
    """
    if mapping is None:
        if require_mapping:
            raise ProductProvenanceError(
                "Product-mode audit requires an explicit product_provenance mapping"
            )
        return None
    schema = mapping.get("schema") or PRODUCT_PROVENANCE_SCHEMA
    if schema != PRODUCT_PROVENANCE_SCHEMA:
        raise ProductProvenanceError(
            f"Unsupported product provenance schema {schema!r}"
        )
    identity = _parse_identity(mapping)
    declared = _parse_mapping_repositories(mapping)
    authorized = {normalize_repository_url(item) for item in facts.authorized_remotes}
    authorized_paths = set(facts.clone_paths)
    if authorized:
        declared_remotes = {row["remote"] for row in declared}
        extra = declared_remotes - authorized
        if extra:
            raise UnauthorizedProductMappingError(
                "product mapping names a repository outside this flow/account"
            )
        missing = authorized - declared_remotes
        if missing:
            raise MismatchedProductMappingError(
                "product mapping omits a configured constituent repository"
            )
        declared_paths = {row["clone_path"] for row in declared}
        if authorized_paths and declared_paths != authorized_paths:
            raise MismatchedProductMappingError(
                "product mapping clone_path set does not match git_clone_config"
            )
    elif declared:
        raise UnauthorizedProductMappingError(
            "product mapping names repositories but this flow has none authorized"
        )

    raw_sbom = mapping.get("sbom")
    sbom_block: Mapping[str, Any] = raw_sbom if isinstance(raw_sbom, Mapping) else {}
    declared_digest = None
    if mapping.get("sbom_digest"):
        declared_digest = parse_sbom_digest(mapping.get("sbom_digest"))
    elif sbom_block.get("digest"):
        declared_digest = parse_sbom_digest(sbom_block.get("digest"))
    declared_path = None
    if isinstance(sbom_block.get("path"), str) and sbom_block["path"].strip():
        declared_path = sbom_block["path"].strip()
    artifact_bytes = facts.sbom_bytes
    artifact_path = facts.sbom_path
    if declared_path and artifact_path and declared_path != artifact_path:
        raise MismatchedProductMappingError(
            "Mapped SBOM path does not match the supplied artifact path"
        )
    sbom_status = "not_supplied"
    sbom_digest = declared_digest
    if artifact_bytes is None:
        if declared_digest is not None:
            sbom_status = "declared_unverified"
        elif declared:
            raise MismatchedProductMappingError(
                "Product mapping requires a supplied SBOM artifact to verify its digest"
            )
    else:
        observed_digest = sha256_digest(artifact_bytes)
        if declared_digest is None:
            sbom_digest = observed_digest
            sbom_status = "verified"
        elif declared_digest != observed_digest:
            raise MismatchedProductMappingError(
                "Mapped SBOM digest does not match the supplied artifact bytes"
            )
        else:
            sbom_digest = observed_digest
            sbom_status = "verified"

    observed_shas = {
        normalize_repository_url(remote): _require_git_sha(sha)
        for remote, sha in facts.clone_shas.items()
    }
    pins = {
        normalize_repository_url(remote): _require_git_sha(sha)
        for remote, sha in (facts.requested_pins or {}).items()
    }
    repositories: list[ProvenanceRepository] = []
    unverified: list[str] = []
    for row in declared:
        status = "declared_unverified"
        observed_sha = observed_shas.get(row["remote"])
        pin_sha = pins.get(row["remote"])
        if observed_sha is not None:
            if observed_sha != row["sha"]:
                raise MismatchedProductMappingError(
                    f"Mapped SHA for {row['clone_path']} does not match the observed checkout"
                )
            status = "verified"
        elif pin_sha is not None:
            if pin_sha != row["sha"]:
                raise MismatchedProductMappingError(
                    f"Mapped SHA for {row['clone_path']} does not match the resolved pin"
                )
            status = "pin_matched"
            unverified.append(
                f"{row['clone_path']}: pin matched the controller-resolved "
                "commit; an unobserved requested SHA is not a verified checkout"
            )
        else:
            unverified.append(
                f"{row['clone_path']}: no trusted checkout SHA; agent-written "
                "SHA is not a build attestation"
            )
        repositories.append(
            ProvenanceRepository(
                remote=row["remote"],
                sha=row["sha"],
                clone_path=row["clone_path"],
                role=row["role"],
                sha_status=status,
            )
        )

    if require_observed and any(repo.sha_status != "verified" for repo in repositories):
        raise MismatchedProductMappingError(
            "Unobserved requested SHA is not a verified checkout"
        )
    if any(repo.sha_status == "declared_unverified" for repo in repositories):
        if len(repositories) > 1:
            raise MismatchedProductMappingError(
                "Product-mode mapping SHAs could not be verified against trusted checkout facts"
            )
        mapping_status = "declared_unverified"
    elif any(repo.sha_status != "verified" for repo in repositories):
        mapping_status = "pin_matched"
        if sbom_status != "verified" and len(repositories) > 1:
            raise MismatchedProductMappingError(
                "Product-mode mapping requires a verified SBOM digest from supplied artifact bytes"
            )
    elif sbom_status != "verified":
        if len(repositories) > 1:
            raise MismatchedProductMappingError(
                "Product-mode mapping requires a verified SBOM digest from supplied artifact bytes"
            )
        mapping_status = "declared_unverified"
    else:
        mapping_status = "verified"

    return VerifiedProductProvenance(
        schema=PRODUCT_PROVENANCE_SCHEMA,
        identity=identity,
        sbom_digest=sbom_digest,
        sbom_path=declared_path or artifact_path,
        sbom_status=sbom_status,
        repositories=tuple(repositories),
        mapping_status=mapping_status,
        unverified_reasons=tuple(unverified),
    )


def publication_approval_required(config: Mapping[str, Any] | None) -> bool:
    """Whether the saved flow requires a human publication approval.

    An optional mapping field is not a policy. Only ``git_clone_config``.
    """
    if not isinstance(config, Mapping):
        return False
    value = config.get("publication_approval")
    return value in {True, "required", "require"}


@dataclass(frozen=True)
class PublicationCandidate:
    """One frozen destination that a writer lease would push.

    ``head_sha`` is the verified candidate, not the original source base.
    """

    repository_url: str
    branch: str
    base: str
    head_sha: str

    def key(self) -> tuple[str, str, str, str]:
        """Exact (repo, branch, base, head) tuple used for approval matching."""
        return (
            normalize_repository_url(self.repository_url),
            self.branch.strip(),
            self.base.strip(),
            _require_git_sha(self.head_sha),
        )


def publication_candidate(value: Any) -> PublicationCandidate:
    """Normalize a frozen binding, mapping, or candidate into one tuple."""
    if isinstance(value, PublicationCandidate):
        return value
    if isinstance(value, Mapping):
        url = value.get("repository_url") or value.get("remote") or ""
        branch = str(value.get("branch") or "").strip()
        base = str(value.get("base") or "").strip()
        head = value.get("head_sha") or value.get("sha") or value.get("commit") or ""
    else:
        url = getattr(value, "repository_url", "") or getattr(value, "remote", "")
        branch = str(getattr(value, "branch", "") or "").strip()
        base = str(getattr(value, "base", "") or "").strip()
        head = (
            getattr(value, "head_sha", None)
            or getattr(value, "sha", None)
            or getattr(value, "commit", None)
            or ""
        )
    if not branch or not base:
        raise ProductProvenanceError(
            "Publication approval cannot be bound without branch and base"
        )
    return PublicationCandidate(
        repository_url=normalize_repository_url(str(url)),
        branch=branch,
        base=base,
        head_sha=_require_git_sha(head),
    )


def _approved_candidate_keys(args: Mapping[str, Any]) -> set[tuple[str, str, str, str]]:
    """Exact candidate tuples from one approval. Unpaired repo/SHA lists never match."""
    raw: Any = None
    for key in ("candidates", "targets", "bindings"):
        value = args.get(key)
        if isinstance(value, list) and value:
            raw = value
            break
    if not isinstance(raw, list):
        return set()
    keys: set[tuple[str, str, str, str]] = set()
    for item in raw:
        try:
            keys.add(publication_candidate(item).key())
        except (ProductProvenanceError, MismatchedProductMappingError, TypeError):
            return set()
    return keys


def authorize_publication_decision(
    records: Sequence[Any],
    *,
    required: bool,
    candidates: Sequence[Any] = (),
    action: str = "isolated_publication",
    now: datetime | None = None,
    repository_urls: Sequence[str] | None = None,
    commits: Sequence[str] | None = None,
) -> None:
    """Refuse publication unless a human approval covers each candidate tuple.

    Bindings are (repository, branch, base, head_sha). Separate repository
    and commit sets are not authority: they would allow swapping pairings.
    Unknown, missing, expired, declined, AI-decided, or unrelated rows deny
    when ``required`` is true. They never authorize. ``repository_urls`` /
    ``commits`` are rejected leftovers; they cannot authorize a write.
    """
    if not required:
        return
    if repository_urls is not None or commits is not None:
        raise ProductProvenanceError(
            "Publication approval cannot use unpaired repository and commit sets"
        )
    try:
        wanted = {publication_candidate(item).key() for item in candidates}
    except (ProductProvenanceError, MismatchedProductMappingError) as exc:
        raise ProductProvenanceError(
            "Publication approval cannot be bound without exact "
            "repository, branch, base, and head scope"
        ) from exc
    if not wanted or any(not item[1] or not item[2] for item in wanted):
        raise ProductProvenanceError(
            "Publication approval cannot be bound without exact "
            "repository, branch, base, and head scope"
        )
    moment = now or datetime.now(timezone.utc)
    for record in records:
        if str(getattr(record, "status", "") or "") != "approved":
            continue
        if getattr(record, "decided_by_ai", False) or getattr(
            record, "auto_approved_reason", None
        ):
            continue
        expires = getattr(record, "expires_at", None)
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= moment:
                continue
        args = getattr(record, "tool_args", None)
        if not isinstance(args, Mapping):
            continue
        named = str(
            args.get("action") or getattr(record, "tool_name", "") or ""
        ).strip()
        if named not in {action, "publish", "isolated_publication"}:
            continue
        approved = _approved_candidate_keys(args)
        if wanted <= approved:
            return
    raise ProductProvenanceError(
        "Publication requires a human platform approval bound to these "
        "repositories, branches, bases, heads, and action"
    )


def enforce_saved_publication_approval(
    db: Any,
    *,
    flow: Any = None,
    account_id: str,
    execution_id: str,
    candidates: Sequence[Any],
    action: str = "isolated_publication",
) -> None:
    """Apply saved ``git_clone_config.publication_approval`` before a write lease.

    Default flows without the opt-in are unchanged. When required, a missing
    flow or unpaired/expired/denied/AI approval refuses the write.
    """
    from sqlalchemy.orm import Session

    from preloop.models.crud import (
        crud_approval_request,
        crud_flow,
        crud_flow_execution,
    )

    if flow is None and isinstance(db, Session):
        execution = crud_flow_execution.get(
            db, id=execution_id, account_id=str(account_id)
        )
        if execution is not None:
            flow = crud_flow.get(
                db, id=str(execution.flow_id), account_id=str(account_id)
            )
    config: Mapping[str, Any] | None = None
    if flow is not None:
        raw = getattr(flow, "git_clone_config", None)
        if isinstance(raw, dict):
            config = raw
        elif raw is not None and hasattr(raw, "model_dump"):
            dumped = raw.model_dump()
            if isinstance(dumped, dict):
                config = dumped
    if not publication_approval_required(config):
        return
    if not isinstance(db, Session) or flow is None:
        raise ProductProvenanceError(
            "Publication requires a human platform approval bound to these "
            "repositories, branches, bases, heads, and action"
        )
    records = crud_approval_request.get_multi_by_execution(
        db,
        execution_id=str(execution_id),
        account_id=str(getattr(flow, "account_id", account_id)),
    )
    authorize_publication_decision(
        records,
        required=True,
        candidates=candidates,
        action=action,
    )


def require_human_publication_approval(
    db: Any,
    *,
    flow: Any = None,
    account_id: str,
    execution_id: str,
    candidates: Sequence[Any],
    action: str = "isolated_publication",
) -> None:
    """Same as ``enforce_saved_publication_approval``, as a publication error."""
    from preloop.services.trusted_publisher import PublicationError

    try:
        enforce_saved_publication_approval(
            db,
            flow=flow,
            account_id=str(account_id),
            execution_id=str(execution_id),
            candidates=candidates,
            action=action,
        )
    except ProductProvenanceError as exc:
        raise PublicationError(str(exc)) from exc


def publication_approval_allows(
    approval: Mapping[str, Any] | None,
    *,
    required_id: str | None,
) -> None:
    """Legacy helper. Do not treat a caller-supplied id as publication policy."""
    del approval, required_id
    raise ProductProvenanceError(
        "A caller-supplied approval id is not publication policy; configure "
        "git_clone_config.publication_approval"
    )

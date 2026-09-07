"""Credential-isolated publication across CRA code + compliance repositories.

Reuses ``publish_verified_bundle`` and per-repository GitHub App leases.
Write credentials never enter the agent environment. Local commits are not
success. One failed remote leaves the multi-repo publication incomplete.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping

import httpx
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_tracker
from preloop.services.product_provenance import (
    ProductProvenanceError,
    clone_path_slug,
    normalize_repository_url,
    publication_candidate,
    require_human_publication_approval,
)
from preloop.services.publication_credentials import (
    mint_repository_lease,
    revoke_repository_lease,
)
from preloop.services.publication_verification import require_verified_publication
from preloop.services.trusted_publisher import (
    MAX_ARCHIVE_BYTES,
    MAX_BUNDLE_BYTES,
    PublicationBinding,
    PublicationError,
    PublicationLease,
    publish_verified_bundle,
    read_publication_bundle,
)
from preloop.utils.pr_metadata import PublicationRecord

_BUNDLE_MEMBER = re.compile(
    r"^(?:\.?/)?(?:evidence/)?repos/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/branch\.bundle$"
)


@dataclass(frozen=True)
class IsolatedPublicationTarget:
    """One authorized publication destination resolved before the agent starts."""

    tracker_id: str
    repository_url: str
    clone_path: str
    role: str
    branch: str
    base: str
    expected_remote_sha: str | None
    base_sha: str
    previous_records: tuple[PublicationRecord, ...] = ()

    @property
    def slug(self) -> str:
        """Archive member slug for this checkout."""
        return clone_path_slug(self.clone_path)

    @property
    def bundle_members(self) -> tuple[str, ...]:
        """Accepted tar member names for this repository's frozen bundle."""
        slug = self.slug
        return (
            f"repos/{slug}/branch.bundle",
            f"evidence/repos/{slug}/branch.bundle",
            f"./repos/{slug}/branch.bundle",
            f"./evidence/repos/{slug}/branch.bundle",
        )


def observed_checkout_shas(policy: Any, archive: bytes | None) -> dict[str, str]:
    """SHAs proven present in frozen bundles. Requested pins are not observed."""
    from preloop.services.publication_worker import inspect_bundle

    targets = policy_targets(policy)
    if not targets or not archive:
        return {}
    if len(targets) > 1:
        bundles = read_named_publication_bundles(archive, targets)
    else:
        bundles = {targets[0].slug: read_publication_bundle(archive)}
    observed: dict[str, str] = {}
    for target in targets:
        bundle = bundles.get(target.slug)
        if not bundle or not target.base_sha:
            continue
        try:
            inspect_bundle(bundle, target.base_sha)
        except PublicationError:
            continue
        observed[target.repository_url] = target.base_sha
    return observed


def is_multi_repo_policy(policy: Any) -> bool:
    """True when isolated publication is bound to more than one repository."""
    targets = getattr(policy, "targets", None) or ()
    return len(tuple(targets)) > 1


def policy_targets(policy: Any) -> tuple[IsolatedPublicationTarget, ...]:
    """Return configured targets, synthesizing the legacy single-repo binding."""
    raw = getattr(policy, "targets", None) or ()
    if raw:
        return tuple(raw)
    url = getattr(policy, "repository_url", None)
    if not url:
        return ()
    return (
        IsolatedPublicationTarget(
            tracker_id=str(getattr(policy, "tracker_id", "")),
            repository_url=str(url),
            clone_path=str(getattr(policy, "clone_path", "workspace") or "workspace"),
            role=str(getattr(policy, "role", "code") or "code"),
            branch=str(getattr(policy, "branch", "")),
            base=str(getattr(policy, "base", "")),
            expected_remote_sha=getattr(policy, "expected_remote_sha", None),
            base_sha=str(getattr(policy, "base_sha", "") or ""),
            previous_records=tuple(getattr(policy, "previous_records", ()) or ()),
        ),
    )


def read_named_publication_bundles(
    archive: bytes, targets: tuple[IsolatedPublicationTarget, ...]
) -> dict[str, bytes]:
    """Read one bounded bundle per authorized target. Extra members are ignored."""
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise PublicationError("Publication archive exceeds transfer limit")
    wanted = {
        member: target.slug for target in targets for member in target.bundle_members
    }
    found: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            expanded = 0
            count = 0
            for member in tar:
                count += 1
                expanded += max(0, member.size)
                if expanded > MAX_BUNDLE_BYTES * 4 or count > 1024:
                    raise PublicationError(
                        "Publication archive exceeds expansion limit"
                    )
                name = member.name
                slug = wanted.get(name)
                if slug is None:
                    match = _BUNDLE_MEMBER.fullmatch(name)
                    if match:
                        raise PublicationError(
                            "Publication archive contains a bundle for a repository "
                            "outside the authorized manifest"
                        )
                    continue
                if slug in found:
                    raise PublicationError(
                        "Publication archive contains duplicate bundles for one repository"
                    )
                if not member.isfile() or member.size > MAX_BUNDLE_BYTES:
                    raise PublicationError(
                        "Publication bundle must be a bounded regular file"
                    )
                stream = tar.extractfile(member)
                if stream is None:
                    raise PublicationError("Publication bundle unreadable")
                found[slug] = stream.read(MAX_BUNDLE_BYTES + 1)
    except (tarfile.TarError, OSError) as exc:
        raise PublicationError("Invalid publication archive") from exc
    missing = [target.slug for target in targets if target.slug not in found]
    if missing:
        raise PublicationError(
            "Publication archive is missing a bundle for an authorized repository"
        )
    return found


def authorize_publication_target(
    target: IsolatedPublicationTarget,
    *,
    authorized_remotes: Mapping[str, str],
    account_id: str,
) -> None:
    """Reject destinations that are not in the prepared account manifest."""
    remote = normalize_repository_url(target.repository_url)
    expected_account = authorized_remotes.get(remote)
    if expected_account != account_id:
        raise PublicationError(
            "Refusing to publish a repository outside the authorized manifest/account"
        )


def empty_receipt(target: IsolatedPublicationTarget, *, error: str) -> dict[str, Any]:
    """Record a failed remote without implying a local-commit success."""
    return {
        "repository_url": target.repository_url,
        "clone_path": target.clone_path,
        "role": target.role,
        "status": "failed",
        "url": None,
        "number": None,
        "branch": target.branch,
        "base": target.base,
        "base_sha": target.base_sha,
        "expected_remote_sha": target.expected_remote_sha,
        "head_sha": None,
        "records": [
            {"execution_id": record.execution_id, "head_sha": record.head_sha}
            for record in target.previous_records
        ],
        "tracker_id": target.tracker_id,
        "error": error,
    }


def published_receipt(
    target: IsolatedPublicationTarget, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Remote commit/PR identity returned by the trusted publisher."""
    records = result.get("records")
    if not isinstance(records, list):
        records = [
            *[
                {"execution_id": record.execution_id, "head_sha": record.head_sha}
                for record in target.previous_records
            ],
            {
                "execution_id": result.get("execution_id"),
                "head_sha": result.get("head_sha"),
            },
        ]
    return {
        "repository_url": target.repository_url,
        "clone_path": target.clone_path,
        "role": target.role,
        "status": "published",
        "url": result.get("url"),
        "number": result.get("number"),
        "branch": result.get("branch") or target.branch,
        "base": result.get("base") or target.base,
        "base_sha": target.base_sha,
        "expected_remote_sha": result.get("head_sha") or target.expected_remote_sha,
        "provider": result.get("provider"),
        "head_sha": result.get("head_sha"),
        "records": records,
        "tracker_id": target.tracker_id,
        "metadata_warnings": result.get("metadata_warnings") or [],
        "error": None,
    }


def aggregate_publication_receipts(
    receipts: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Combine per-repo remote receipts. Incomplete unless every repo published."""
    rows = list(receipts)
    published = [row for row in rows if row.get("status") == "published"]
    failed = [row for row in rows if row.get("status") != "published"]
    if failed:
        status = "partial" if published else "failed"
        complete = False
    else:
        status = "complete"
        complete = True
    body: dict[str, Any] = {
        "status": status,
        "complete": complete,
        "repositories": rows,
    }
    if rows:
        body["bindings"] = [
            {
                "repository_url": row.get("repository_url"),
                "clone_path": row.get("clone_path"),
                "branch": row.get("branch"),
                "base": row.get("base"),
                "base_sha": row.get("base_sha"),
            }
            for row in rows
        ]
    if len(rows) == 1 and published:
        only = published[0]
        body.update(
            {
                "url": only.get("url"),
                "number": only.get("number"),
                "branch": only.get("branch"),
                "provider": only.get("provider"),
                "head_sha": only.get("head_sha"),
                "metadata_warnings": only.get("metadata_warnings") or [],
            }
        )
    return body


async def publish_one_isolated_target(
    *,
    db: Session,
    policy: Any,
    target: IsolatedPublicationTarget,
    bundle: bytes,
    agent_result: Mapping[str, Any],
    verification: Any,
    client: httpx.AsyncClient,
    flow: Any = None,
) -> dict[str, Any]:
    """Verify and publish one authorized repository using existing machinery."""
    account_id = str(policy.account_id)
    authorize_publication_target(
        target,
        authorized_remotes={
            normalize_repository_url(item.repository_url): account_id
            for item in policy_targets(policy)
        },
        account_id=account_id,
    )
    head_sha = require_verified_publication(
        verification, execution_id=policy.execution_id, bundle=bundle
    )
    records = (
        *target.previous_records,
        PublicationRecord(policy.execution_id, head_sha),
    )
    binding = PublicationBinding(
        target.repository_url,
        target.branch,
        target.base,
        head_sha,
        target.expected_remote_sha,
        records,
        settings.preloop_url,
        "github",
        policy.configured_title,
        policy.configured_body,
        policy.issue_number,
    )
    tracker = crud_tracker.get_by_id_and_account(
        db, id=target.tracker_id, account_id=account_id
    )
    if tracker is None:
        raise PublicationError(
            "Publication tracker was removed or is no longer authorized"
        )
    require_human_publication_approval(
        db,
        flow=flow,
        account_id=str(account_id),
        execution_id=str(policy.execution_id),
        candidates=[binding],
    )
    write_lease: PublicationLease | None = None

    async def acquire() -> PublicationLease:
        nonlocal write_lease
        write_lease = await mint_repository_lease(
            tracker, target.repository_url, write=True, client=client
        )
        return write_lease

    try:
        result = await publish_verified_bundle(
            binding=binding,
            bundle=bundle,
            result_json=json.dumps(
                agent_result.get("result"), ensure_ascii=False
            ).encode(),
            acquire_lease=acquire,
            client=client,
        )
    finally:
        if write_lease is not None:
            await revoke_repository_lease(write_lease, client)
    result.update(
        {
            "repository_url": target.repository_url,
            "base": target.base,
            "records": [
                {"execution_id": record.execution_id, "head_sha": record.head_sha}
                for record in records
            ],
        }
    )
    return dict(result)


async def finish_multi_repo_isolated_publication(
    *,
    db: Session,
    policy: Any,
    agent_result: dict[str, Any],
    archive: bytes | None,
    verify: Callable[[Any, bytes], Awaitable[Any]],
    flow: Any = None,
) -> dict[str, Any]:
    """Publish every authorized repo. Stop marking complete on the first gap.

    Already-published remotes are idempotent inside ``publish_verified_bundle``
    (expected-head match). Failed remotes stay failed; successful remotes keep
    their receipts so a retry can complete the remainder.
    """
    targets = policy_targets(policy)
    if not targets:
        raise PublicationError("Isolated publication has no authorized repositories")
    if len(targets) == 1:
        bundle = read_publication_bundle(archive or b"")
        hosted = await verify(replace(policy, base_sha=targets[0].base_sha), bundle)
        async with httpx.AsyncClient() as client:
            result = await publish_one_isolated_target(
                db=db,
                policy=policy,
                target=targets[0],
                bundle=bundle,
                agent_result=agent_result,
                verification=getattr(hosted, "verification", hosted),
                client=client,
                flow=flow,
            )
        return result

    bundles = read_named_publication_bundles(archive or b"", targets)
    receipts: list[dict[str, Any]] = []
    pending: list[tuple[IsolatedPublicationTarget, bytes, Any]] = []
    for target in targets:
        bundle = bundles[target.slug]
        try:
            hosted = await verify(replace(policy, base_sha=target.base_sha), bundle)
            pending.append((target, bundle, getattr(hosted, "verification", hosted)))
        except PublicationError as exc:
            receipts.append(empty_receipt(target, error=str(exc)))
    if pending:
        candidates = []
        try:
            for target, bundle, verification in pending:
                head_sha = require_verified_publication(
                    verification,
                    execution_id=policy.execution_id,
                    bundle=bundle,
                )
                candidates.append(
                    publication_candidate(
                        {
                            "repository_url": target.repository_url,
                            "branch": target.branch,
                            "base": target.base,
                            "head_sha": head_sha,
                        }
                    )
                )
            require_human_publication_approval(
                db,
                flow=flow,
                account_id=str(policy.account_id),
                execution_id=str(policy.execution_id),
                candidates=candidates,
            )
        except (PublicationError, ProductProvenanceError) as exc:
            for target, _, _ in pending:
                receipts.append(empty_receipt(target, error=str(exc)))
            pending = []
    async with httpx.AsyncClient() as client:
        for target, bundle, verification in pending:
            try:
                result = await publish_one_isolated_target(
                    db=db,
                    policy=policy,
                    target=target,
                    bundle=bundle,
                    agent_result=agent_result,
                    verification=verification,
                    client=client,
                    flow=flow,
                )
                receipts.append(published_receipt(target, result))
            except PublicationError as exc:
                receipts.append(empty_receipt(target, error=str(exc)))
    aggregated = aggregate_publication_receipts(receipts)
    if not aggregated["complete"]:
        raise IncompleteMultiRepoPublicationError(aggregated)
    return aggregated


class IncompleteMultiRepoPublicationError(PublicationError):
    """Partial remote publication: do not treat the product pack as complete."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        failed = [
            row.get("clone_path") or row.get("repository_url")
            for row in receipt.get("repositories") or []
            if row.get("status") != "published"
        ]
        super().__init__(
            "Multi-repo publication is incomplete; remote failure for: "
            + ", ".join(str(item) for item in failed)
        )
        self.receipt = dict(receipt)


def repository_role(clone_path: str, payload: Mapping[str, Any] | None) -> str:
    """Identify the compliance checkout from flow convention or payload override."""
    slug = clone_path_slug(clone_path)
    override = None
    if isinstance(payload, Mapping):
        raw = payload.get("compliance_repo_path")
        if isinstance(raw, str) and raw.strip():
            override = clone_path_slug(raw)
    if override and slug == override:
        return "compliance"
    if slug == "compliance":
        return "compliance"
    return "code"

"""Control-plane lifecycle for isolated publication.

The context sent to the agent contains only the read lease. The private policy
object stays in the orchestrator and is never serialized into a runner job.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_flow_execution, crud_project, crud_tracker
from preloop.services.publication_credentials import (
    mint_repository_lease,
    revoke_repository_lease,
    validate_publication_tracker,
)
from preloop.services.multi_repo_publication import (
    IsolatedPublicationTarget,
    finish_multi_repo_isolated_publication,
    is_multi_repo_policy,
    repository_role,
)
from preloop.services.product_provenance import is_git_sha, normalize_repository_url
from preloop.services.trusted_publisher import (
    PublicationBinding,
    PublicationError,
    PublicationLease,
    publish_verified_bundle,
    read_publication_bundle,
)
from preloop.models.schemas.verification import ResolvedVerificationPolicy
from preloop.services.verification import resolve_verification_policy
from preloop.utils.pr_metadata import PublicationRecord


@dataclass(frozen=True)
class IsolatedPublicationPolicy:
    """Trusted destination snapshot retained outside the agent context."""

    tracker_id: str
    account_id: str
    repository_url: str
    branch: str
    base: str
    expected_remote_sha: str | None
    execution_id: str
    previous_records: tuple[PublicationRecord, ...]
    read_lease: PublicationLease | None
    configured_title: str
    configured_body: str
    issue_number: str
    base_sha: str
    verification_policy: ResolvedVerificationPolicy
    verification_image: str
    private: bool = False
    nonce: str = ""
    targets: tuple[IsolatedPublicationTarget, ...] = ()
    read_leases: tuple[PublicationLease, ...] = ()


def isolated_publication_enabled(config: Any) -> bool:
    """Whether a saved flow explicitly opts into the credential boundary."""
    return isinstance(config, dict) and config.get("publication_mode") == "isolated"


def _prior_binding_for_remote(
    prior_publication: dict[str, Any] | None, repository_url: str
) -> dict[str, Any] | None:
    """Select the prior receipt for one remote from single- or multi-repo records."""
    if not isinstance(prior_publication, dict):
        return None
    rows = prior_publication.get("repositories")
    if isinstance(rows, list) and rows:
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("repository_url") == repository_url:
                return dict(row)
        return None
    if prior_publication.get("repository_url") == repository_url:
        return dict(prior_publication)
    return None


def resume_topology_matches(
    prior_publication: dict[str, Any],
    repositories: list[dict[str, Any]],
) -> None:
    """Refuse adding, removing, or remapping remotes/clone paths on resume."""
    from preloop.services.product_provenance import clone_path_slug

    rows = prior_publication.get("repositories")
    prior_rows: list[dict[str, Any]]
    if isinstance(rows, list) and rows:
        prior_rows = [row for row in rows if isinstance(row, dict)]
    elif prior_publication.get("repository_url"):
        prior_rows = [prior_publication]
    else:
        raise PublicationError(
            "Continuation requires a trusted publication binding; legacy PRs must be explicitly migrated"
        )
    prior_ids = {
        (
            normalize_repository_url(str(row["repository_url"])),
            clone_path_slug(str(row.get("clone_path") or "workspace")),
        )
        for row in prior_rows
        if row.get("repository_url")
    }
    current_ids = {
        (
            normalize_repository_url(str(row["repository_url"])),
            clone_path_slug(
                str(row.get("clone_path") or f"workspace-{index + 1}")
                if index
                else str(row.get("clone_path") or "workspace")
            ),
        )
        for index, row in enumerate(repositories)
        if row.get("repository_url")
    }
    if prior_ids != current_ids:
        raise PublicationError(
            "Resume cannot add, remove, or remap constituent repositories"
        )


async def _bind_isolated_repository(
    db: Session,
    *,
    flow: models.Flow,
    context: dict[str, Any],
    repository: dict[str, Any],
    index: int,
    branch: str,
    resume: bool,
    prior_publication: dict[str, Any] | None,
    config: dict[str, Any],
    payload: dict[str, Any],
    execution_id: str,
    client: httpx.AsyncClient,
) -> tuple[
    IsolatedPublicationTarget,
    dict[str, Any],
    PublicationLease,
    str,
    str | None,
    tuple[PublicationRecord, ...],
]:
    """Resolve one account-owned repository and mint a read-only clone lease."""
    project_id = repository.get("project_id") or (
        context.get("trigger_project_id") if index == 0 else None
    )
    project = crud_project.get(db, id=str(project_id)) if project_id else None
    tracker_id = repository.get("tracker_id") or (
        str(project.organization.tracker_id)
        if project and project.organization
        else None
    )
    tracker = (
        crud_tracker.get_by_id_and_account(
            db, id=str(tracker_id), account_id=str(flow.account_id)
        )
        if tracker_id
        else None
    )
    if tracker is None:
        raise PublicationError(
            "Isolated publication requires an account-owned tracker/project binding"
        )
    validate_publication_tracker(tracker)
    repository_url = repository.get("repository_url")
    if (
        not repository_url
        and project
        and project.organization
        and str(project.organization.tracker_id) == str(tracker.id)
        and project.slug
    ):
        repository_url = f"https://github.com/{project.slug}.git"
    if not repository_url:
        raise PublicationError(
            "Configure a repository URL or trusted project for isolated publication; webhook clone URLs are not publication authority"
        )
    repository_url = normalize_repository_url(str(repository_url))
    project_path = repository_url.removeprefix("https://github.com/").removesuffix(
        ".git"
    )
    prior_binding = _prior_binding_for_remote(prior_publication, repository_url)
    previous_records: tuple[PublicationRecord, ...] = ()
    repo_branch = branch
    if resume:
        if prior_binding is None:
            raise PublicationError(
                "Continuation requires a trusted publication binding; legacy PRs must be explicitly migrated"
            )
        raw_records = prior_binding.get("records")
        if isinstance(raw_records, list) and raw_records:
            previous_records = tuple(
                PublicationRecord(**record) for record in raw_records
            )
        if isinstance(prior_binding.get("branch"), str) and prior_binding["branch"]:
            repo_branch = str(prior_binding["branch"])
    clone_path = str(
        repository.get("clone_path")
        or ("workspace" if index == 0 else f"workspace-{index + 1}")
    )
    role = repository_role(clone_path, payload)
    read_lease = await mint_repository_lease(
        tracker, repository_url, write=False, client=client
    )
    headers = {"Authorization": f"Bearer {read_lease.token}"}
    try:
        response = await client.get(
            f"https://api.github.com/repos/{project_path}",
            headers=headers,
            timeout=30,
            follow_redirects=False,
        )
        response.raise_for_status()
        info = response.json()
        if info.get("full_name") != project_path:
            raise PublicationError(
                "Resolved repository does not match publication binding"
            )
        configured_base = repository.get("source_branch") or repository.get("branch")
        if resume and prior_binding is not None and prior_binding.get("base"):
            base = str(prior_binding["base"])
        else:
            base = (
                configured_base or config.get("source_branch") or info["default_branch"]
            )
        PublicationBinding(
            repository_url,
            repo_branch,
            base,
            "0" * 40,
            None,
            (PublicationRecord(execution_id, "0" * 40),),
            settings.preloop_url,
            "github",
        )
        pinned = None
        if (
            resume
            and prior_binding is not None
            and is_git_sha(prior_binding.get("base_sha"))
        ):
            pinned = str(prior_binding["base_sha"]).lower()
        if pinned is None:
            response = await client.get(
                f"https://api.github.com/repos/{project_path}/git/ref/heads/{base}",
                headers=headers,
                timeout=30,
                follow_redirects=False,
            )
            response.raise_for_status()
            base_sha = response.json()["object"]["sha"]
            if not isinstance(base_sha, str) or not re.fullmatch(
                r"[a-f0-9]{40}", base_sha
            ):
                raise PublicationError(
                    "Provider did not resolve an exact trusted base commit"
                )
        else:
            base_sha = pinned
        response = await client.get(
            f"https://api.github.com/repos/{project_path}/git/ref/heads/{repo_branch}",
            headers=headers,
            timeout=30,
            follow_redirects=False,
        )
        if response.status_code == 404:
            expected_remote = None
        else:
            response.raise_for_status()
            expected_remote = response.json()["object"]["sha"]
        if resume and prior_binding is not None:
            published_head = prior_binding.get("head_sha")
            if prior_binding.get("status") == "published" and is_git_sha(
                published_head
            ):
                expected_remote = str(published_head).lower()
            elif is_git_sha(prior_binding.get("expected_remote_sha")):
                expected_remote = str(prior_binding["expected_remote_sha"]).lower()
        if not resume and expected_remote is not None:
            raise PublicationError(
                "Configured publication branch already exists; use a unique target branch or resume its bound execution"
            )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        await revoke_repository_lease(read_lease, client)
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError(
            "Could not resolve isolated publication repository/base/head"
        ) from exc
    target = IsolatedPublicationTarget(
        tracker_id=str(tracker.id),
        repository_url=repository_url,
        clone_path=clone_path,
        role=role,
        branch=repo_branch,
        base=base,
        expected_remote_sha=expected_remote,
        base_sha=base_sha,
        previous_records=previous_records,
    )
    resolved = {
        **repository,
        "repository_url": repository_url,
        "tracker_id": str(tracker.id),
        "clone_path": clone_path,
        "source_branch": base,
        "target_branch": repo_branch,
        "commit": base_sha if not resume else (expected_remote or base_sha),
        "pin_sha": base_sha,
    }
    return target, resolved, read_lease, base, expected_remote, previous_records


async def prepare_isolated_publication(
    db: Session, flow: models.Flow, context: dict[str, Any]
) -> IsolatedPublicationPolicy:
    """Resolve authorized repository and readonly clone lease before agent start."""
    from preloop.agents.container import (
        extract_issue_number_from_trigger,
        interpolate_git_config_text,
    )
    from preloop.services.runner_service import resolve_runner_pool

    private = bool(resolve_runner_pool(flow, context, db=db))
    if private:
        from preloop.services.private_publication import restore_private_publication

        restored = await restore_private_publication(db, flow, context)
        if restored is not None:
            return restored
    config = dict(context["git_clone_config"])
    verification_policy = resolve_verification_policy(config)
    if verification_policy.mode != "gate" or verification_policy.profile is None:
        raise PublicationError(
            "Isolated publication requires a trusted verification profile"
        )
    verification_image = (config.get("verification") or {}).get("image", "")
    if not isinstance(verification_image, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}", verification_image
    ):
        raise PublicationError(
            "Isolated verification requires a digest-pinned generic toolchain image containing the configured check dependencies"
        )
    repositories = [dict(row) for row in (config.get("repositories") or [])]
    if not repositories:
        repositories = [{}]
    trigger = context.get("trigger_event_data") or {}
    payload = (
        trigger.get("payload") if isinstance(trigger.get("payload"), dict) else trigger
    )
    issue_number = extract_issue_number_from_trigger(trigger) or ""
    execution_id = str(context["execution_id"])
    branch = config.get("target_branch") or (
        f"preloop/issue-{issue_number}-{execution_id[:8]}"
        if issue_number
        else f"preloop/flow-{execution_id[:8]}"
    )
    resume = trigger.get("_resume") or {}
    prior_publication = None
    if resume:
        prior = crud_flow_execution.get(
            db, id=resume.get("execution_id"), account_id=str(flow.account_id)
        )
        if prior is None or str(prior.flow_id) != str(flow.id):
            raise PublicationError(
                "Isolated continuation requires a prior execution of this flow"
            )
        prior_result: dict[str, Any] = (
            prior.result if isinstance(prior.result, dict) else {}
        )
        prior_publication = prior_result.get("trusted_publication")
        if not isinstance(prior_publication, dict):
            raise PublicationError(
                "Continuation requires a trusted publication binding; legacy PRs must be explicitly migrated"
            )
        resume_topology_matches(prior_publication, repositories)
        prior_rows = prior_publication.get("repositories")
        if isinstance(prior_rows, list) and prior_rows:
            first_row = prior_rows[0] if isinstance(prior_rows[0], dict) else {}
            branch = str(
                first_row.get("branch") or prior_publication.get("branch") or branch
            )
        elif prior_publication.get("branch"):
            branch = str(prior_publication["branch"])

    targets: list[IsolatedPublicationTarget] = []
    resolved_repos: list[dict[str, Any]] = []
    leases: list[PublicationLease] = []
    credentials: dict[str, dict[str, str]] = {}
    first_base = ""
    first_expected: str | None = None
    first_previous: tuple[PublicationRecord, ...] = ()

    async with httpx.AsyncClient() as client:
        try:
            for index, repository in enumerate(repositories):
                (
                    target,
                    resolved,
                    lease,
                    base,
                    expected_remote,
                    previous_records,
                ) = await _bind_isolated_repository(
                    db,
                    flow=flow,
                    context=context,
                    repository=repository,
                    index=index,
                    branch=branch,
                    resume=bool(resume),
                    prior_publication=prior_publication,
                    config=config,
                    payload=payload if isinstance(payload, dict) else {},
                    execution_id=execution_id,
                    client=client,
                )
                targets.append(target)
                resolved_repos.append(resolved)
                leases.append(lease)
                credentials[normalize_repository_url(target.repository_url)] = {
                    "token": lease.token,
                    "tracker_type": "github",
                    "permission": "read",
                }
                if str(target.tracker_id) not in credentials:
                    credentials[str(target.tracker_id)] = credentials[
                        normalize_repository_url(target.repository_url)
                    ]
                if index == 0:
                    first_base = base
                    first_expected = expected_remote
                    first_previous = previous_records
        except Exception:
            for lease in leases:
                await revoke_repository_lease(lease, client)
            raise

    primary = targets[0]
    credentials.setdefault(
        str(primary.tracker_id),
        {
            "token": leases[0].token,
            "tracker_type": "github",
            "permission": "read",
        },
    )
    config["repositories"] = resolved_repos
    config["source_branch"] = first_base
    config["target_branch"] = primary.branch
    context["git_clone_config"] = config
    context["git_credentials_map"] = credentials
    context["trigger_tracker_id"] = str(primary.tracker_id)
    if resume:
        context["trigger_event_data"] = {
            **trigger,
            "_resume": {**resume, "source_branch": primary.branch},
        }
    return IsolatedPublicationPolicy(
        str(primary.tracker_id),
        str(flow.account_id),
        primary.repository_url,
        primary.branch,
        first_base,
        first_expected,
        execution_id,
        first_previous,
        leases[0],
        interpolate_git_config_text(config.get("pull_request_title"), trigger),
        interpolate_git_config_text(config.get("pull_request_description"), trigger),
        issue_number,
        primary.base_sha,
        verification_policy,
        verification_image,
        private,
        secrets.token_hex(32),
        tuple(targets),
        tuple(leases),
    )


async def finish_isolated_publication(
    db: Session,
    policy: IsolatedPublicationPolicy,
    agent_result: dict[str, Any],
    archive: bytes | None,
    verification: Any,
    verify: Any = None,
) -> dict[str, Any]:
    """Publish only after a trusted verifier returns an exact artifact binding."""
    # #428 adapter supplies this *control-plane* value; agent result.json must
    # never populate it. It binds the exact bytes, commit and execution.
    from preloop.services.publication_verification import require_verified_publication

    if is_multi_repo_policy(policy):
        if verify is None:
            raise PublicationError(
                "Multi-repo isolated publication requires per-repository verification"
            )
        return await finish_multi_repo_isolated_publication(
            db=db,
            policy=policy,
            agent_result=agent_result,
            archive=archive,
            verify=verify,
        )

    bundle = read_publication_bundle(archive or b"")
    head_sha = require_verified_publication(
        verification, execution_id=policy.execution_id, bundle=bundle
    )
    records = (
        *policy.previous_records,
        PublicationRecord(policy.execution_id, head_sha),
    )
    binding = PublicationBinding(
        policy.repository_url,
        policy.branch,
        policy.base,
        head_sha,
        policy.expected_remote_sha,
        records,
        settings.preloop_url,
        "github",
        policy.configured_title,
        policy.configured_body,
        policy.issue_number,
    )
    tracker = crud_tracker.get_by_id_and_account(
        db, id=policy.tracker_id, account_id=policy.account_id
    )
    if tracker is None:
        raise PublicationError(
            "Publication tracker was removed or is no longer authorized"
        )
    async with httpx.AsyncClient() as client:
        write_lease = None

        async def acquire() -> PublicationLease:
            nonlocal write_lease
            write_lease = await mint_repository_lease(
                tracker, policy.repository_url, write=True, client=client
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
                "repository_url": policy.repository_url,
                "base": policy.base,
                "records": [
                    {"execution_id": record.execution_id, "head_sha": record.head_sha}
                    for record in records
                ],
            }
        )
        return result

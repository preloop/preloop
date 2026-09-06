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
    read_lease: PublicationLease
    configured_title: str
    configured_body: str
    issue_number: str
    base_sha: str
    verification_policy: ResolvedVerificationPolicy
    verification_image: str
    private: bool = False
    nonce: str = ""


def isolated_publication_enabled(config: Any) -> bool:
    """Whether a saved flow explicitly opts into the credential boundary."""
    return isinstance(config, dict) and config.get("publication_mode") == "isolated"


async def prepare_isolated_publication(
    db: Session, flow: models.Flow, context: dict[str, Any]
) -> IsolatedPublicationPolicy:
    """Resolve authorized repository and readonly clone lease before agent start."""
    from preloop.agents.container import (
        extract_issue_number_from_trigger,
        interpolate_git_config_text,
    )
    from preloop.services.runner_service import resolve_runner_pool

    if resolve_runner_pool(flow, context, db=db):
        raise PublicationError(
            "Isolated publication on private runners requires trusted bundle/evidence upload support; use a hosted isolated runtime until that capability is available"
        )
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
    repositories = config.get("repositories") or []
    if len(repositories) > 1:
        raise PublicationError(
            "Isolated publication currently supports one bound repository per flow"
        )
    repository = dict(repositories[0]) if repositories else {}
    project_id = repository.get("project_id") or context.get("trigger_project_id")
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
    project_path = (
        str(repository_url).removeprefix("https://github.com/").removesuffix(".git")
    )
    trigger = context.get("trigger_event_data") or {}
    issue_number = extract_issue_number_from_trigger(trigger) or ""
    execution_id = str(context["execution_id"])
    branch = config.get("target_branch") or (
        f"preloop/issue-{issue_number}-{execution_id[:8]}"
        if issue_number
        else f"preloop/flow-{execution_id[:8]}"
    )
    previous_records: tuple[PublicationRecord, ...] = ()
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
        prior_publication = (prior.result or {}).get("trusted_publication")
        if (
            not isinstance(prior_publication, dict)
            or prior_publication.get("repository_url") != repository_url
        ):
            raise PublicationError(
                "Continuation requires a trusted publication binding; legacy PRs must be explicitly migrated"
            )
        branch = prior_publication["branch"]
        previous_records = tuple(
            PublicationRecord(**record) for record in prior_publication["records"]
        )
    async with httpx.AsyncClient() as client:
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
            base = (
                (prior_publication or {}).get("base")
                or config.get("source_branch")
                or info["default_branch"]
            )
            # Validate branch/URL before interpolating into provider requests.
            PublicationBinding(
                repository_url,
                branch,
                base,
                "0" * 40,
                None,
                (PublicationRecord(execution_id, "0" * 40),),
                settings.preloop_url,
                "github",
            )
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
            response = await client.get(
                f"https://api.github.com/repos/{project_path}/git/ref/heads/{branch}",
                headers=headers,
                timeout=30,
                follow_redirects=False,
            )
            if response.status_code == 404:
                expected_remote = None
            else:
                response.raise_for_status()
                expected_remote = response.json()["object"]["sha"]
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
    config["repositories"] = [
        {**repository, "repository_url": repository_url, "tracker_id": str(tracker.id)}
    ]
    config["source_branch"] = base
    config["target_branch"] = branch
    context["git_clone_config"] = config
    context["git_credentials_map"] = {
        str(tracker.id): {
            "token": read_lease.token,
            "tracker_type": "github",
            "permission": "read",
        }
    }
    context["trigger_tracker_id"] = str(tracker.id)
    # Only this validated binding may control resume checkout branches.
    if resume:
        context["trigger_event_data"] = {
            **trigger,
            "_resume": {**resume, "source_branch": branch},
        }
    return IsolatedPublicationPolicy(
        str(tracker.id),
        str(flow.account_id),
        repository_url,
        branch,
        base,
        expected_remote,
        execution_id,
        previous_records,
        read_lease,
        interpolate_git_config_text(config.get("pull_request_title"), trigger),
        interpolate_git_config_text(config.get("pull_request_description"), trigger),
        issue_number,
        base_sha,
        verification_policy,
        verification_image,
        False,
        secrets.token_hex(32),
    )


async def finish_isolated_publication(
    db: Session,
    policy: IsolatedPublicationPolicy,
    agent_result: dict[str, Any],
    archive: bytes | None,
    verification: Any,
) -> dict[str, Any]:
    """Publish only after a trusted verifier returns an exact artifact binding."""
    # #428 adapter supplies this *control-plane* value; agent result.json must
    # never populate it. It binds the exact bytes, commit and execution.
    from preloop.services.publication_verification import require_verified_publication

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

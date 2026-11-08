"""Publish a restricted CI review from a Preloop repository_dispatch.

Runs in a GitHub Actions job triggered by ``repository_dispatch``. The job
holds the only GitHub-write credential (its own ``GITHUB_TOKEN`` with
``checks: write`` and ``pull-requests: write``); the Preloop reviewer never
does. The script never polls and never executes pull-request code:

1. Verify the signed completion inside ``client_payload`` with the
   subscription signing secret, and check its freshness.
2. Re-read the execution and result from Preloop with a read-scoped
   restricted CI token and require every correlation field, the recorded
   base commit and the result digest to match the signed completion.
3. Read the pull request with ``GITHUB_TOKEN``. If its head or base moved,
   the review is stale: publish a ``neutral`` check run and no review.
4. Otherwise publish one COMMENT review on the exact head and one check run,
   both deduplicated, so a redelivered dispatch publishes nothing new.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))

from restricted_ci_review import (  # noqa: E402
    CORRELATION,
    TERMINAL,
    VerificationError,
    json_digest,
    request_json,
    verify_callback,
    verify_execution,
)

CHECK_NAME = "Preloop review"
# A dispatch can wait in GitHub's queue before the job starts, so the
# freshness window is wider than the 5 minute window for direct callbacks.
# Replays inside it are harmless: publication is deduplicated per execution.
DEFAULT_TOLERANCE_SECONDS = 1800
MAX_REVIEW_CHARS = 60000
SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

Fetch = Callable[..., Any]


def read_client_payload(event_path: str) -> dict[str, Any]:
    """Load ``client_payload`` from the Actions event file, never from argv."""
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VerificationError("Dispatch event is unreadable") from None
    payload = event.get("client_payload") if isinstance(event, dict) else None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("event"), str)
        or not isinstance(payload.get("signature"), str)
    ):
        raise VerificationError("Dispatch payload is not a Preloop completion")
    return payload


def verify_dispatch(
    payload: dict[str, Any],
    secret: str,
    persisted: dict[str, Any],
    *,
    now: int,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    expected_account_id: str | None = None,
) -> dict[str, Any]:
    """Verify the signed envelope bytes and their persisted completion.

    The signature covers ``t.<event>``, where ``event`` is the exact envelope
    string Preloop signed, so nothing is re-serialized before verifying.
    """
    match = re.fullmatch(r"t=([0-9]+),v1=[0-9a-f]{64}", payload["signature"])
    if match is None or abs(now - int(match[1])) > tolerance:
        raise VerificationError("Dispatch signature is invalid or stale")
    # verify_callback applies its own 300 second window; evaluate it at the
    # signing time once the wider dispatch window has been enforced here.
    data = verify_callback(
        payload["event"].encode("utf-8"),
        payload["signature"],
        secret,
        persisted,
        now=int(match[1]),
        expected_account_id=expected_account_id,
    )
    if json.loads(payload["event"])["id"] != payload.get("event_id"):
        raise VerificationError("Dispatch event identity mismatch")
    return data


def verify_binding(
    data: dict[str, Any],
    execution: dict[str, Any],
    result: dict[str, Any],
    *,
    repository: str,
    flow: str,
    project: str,
) -> None:
    """Require the signed binding, the execution and the result to agree."""
    if (
        data.get("tracker_type") != "github"
        or data.get("repository_slug") != repository
        or data.get("flow_id") != flow
        or data.get("project_id") != project
    ):
        raise VerificationError("Completion is not bound to this repository/flow")
    base = data.get("base_sha")
    if not isinstance(base, str) or not SHA.fullmatch(base):
        raise VerificationError("Completion has no recorded base revision")
    expected = {field: data[field] for field in CORRELATION}
    verify_execution(execution, expected, data["execution_id"])
    verify_execution(
        {**result, "id": result.get("execution_id")}, expected, data["execution_id"]
    )
    if (
        execution.get("status") != data["status"]
        or result.get("status") != data["status"]
        or execution.get("base_sha") != base
        or result.get("base_sha") != base
        or result.get("result_withheld_paths") not in ([], None)
        or json_digest(result.get("result")) != data.get("result_sha256")
    ):
        raise VerificationError("Persisted result differs from the completion")


def pr_is_current(pr: dict[str, Any], data: dict[str, Any]) -> bool:
    """Whether the PR still has the exact reviewed head and base."""
    repository = str(data["repository_identifier"])
    return (
        pr.get("state") == "open"
        and str(pr.get("id")) == data["provider_pr_id"]
        and pr.get("number") == data["pr_number"]
        and pr.get("head", {}).get("sha") == data["head_sha"]
        and pr.get("base", {}).get("sha") == data["base_sha"]
        and all(
            str(pr.get(side, {}).get("repo", {}).get("id")) == repository
            for side in ("head", "base")
        )
    )


def _existing_check(github: Fetch, repository: str, data: dict[str, Any]) -> Any:
    found = github(
        f"/repos/{repository}/commits/{data['head_sha']}/check-runs"
        f"?check_name={CHECK_NAME.replace(' ', '%20')}&filter=all&per_page=100"
    )
    for run in found.get("check_runs", []) if isinstance(found, dict) else []:
        if run.get("external_id") == data["execution_id"]:
            return run
    return None


def publish_check(
    github: Fetch,
    repository: str,
    data: dict[str, Any],
    *,
    conclusion: str,
    title: str,
    summary: str,
) -> Any:
    """Create one completed check run per execution, deduplicated."""
    existing = _existing_check(github, repository, data)
    if existing is not None:
        return existing
    return github(
        f"/repos/{repository}/check-runs",
        "POST",
        {
            "name": CHECK_NAME,
            "head_sha": data["head_sha"],
            "external_id": data["execution_id"],
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary[:MAX_REVIEW_CHARS]},
        },
    )


def publish_review(
    github: Fetch, repository: str, data: dict[str, Any], review: str
) -> Any:
    """Post one COMMENT review on the exact head, deduplicated by marker."""
    marker = f"<!-- preloop-ci:{data['execution_id']}:{data['head_sha']} -->"
    path = f"/repos/{repository}/pulls/{data['pr_number']}/reviews"
    for page in range(1, 101):
        reviews = github(f"{path}?per_page=100&page={page}")
        for item in reviews:
            if marker in (item.get("body") or "") and (
                item.get("commit_id") == data["head_sha"]
            ):
                return item
        if len(reviews) < 100:
            break
    else:
        raise VerificationError("Review history exceeds safe deduplication bound")
    return github(
        path,
        "POST",
        {
            "commit_id": data["head_sha"],
            "event": "COMMENT",
            "body": review[:MAX_REVIEW_CHARS] + "\n\n" + marker,
        },
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Verify one dispatch end to end and publish at most one review/check."""
    token = os.environ.get("PRELOOP_CI_TOKEN", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    secret = os.environ.get("PRELOOP_CALLBACK_SECRET", "")
    if not token.startswith("ci_") or not github_token or not secret:
        raise VerificationError(
            "Use a restricted CI token, GITHUB_TOKEN and the signing secret"
        )
    UUID(args.flow)
    UUID(args.project)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise VerificationError("Use an owner/repository slug")

    # Actions sets GITHUB_API_URL (``<host>/api/v3`` on GitHub Enterprise
    # Server); request_json rejects anything but a plain HTTPS origin.
    github_api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"

    def github(path: str, method: str = "GET", body: Any = None) -> Any:
        return request_json(github_api, path, github_token, method, body)

    def remote(path: str) -> Any:
        return request_json(args.url, "/api/v1" + path, token)

    payload = read_client_payload(args.event_path)
    try:
        unsigned = json.loads(payload["event"])["data"]
        execution_id = str(UUID(unsigned["execution_id"]))
    except (ValueError, KeyError, TypeError):
        raise VerificationError(
            "Dispatch payload is not a Preloop completion"
        ) from None
    # Re-fetch with the read-scoped CI token before trusting anything signed:
    # the signature proves origin, the API proves the current persisted state.
    execution = remote(f"/flows/executions/{execution_id}")
    if execution.get("status") not in TERMINAL:
        raise VerificationError("Execution is not terminal")
    result = remote(f"/flows/executions/{execution_id}/result")
    data = verify_dispatch(
        payload,
        secret,
        result,
        now=int(time.time()),
        tolerance=args.tolerance,
        expected_account_id=args.account or None,
    )
    verify_binding(
        data,
        execution,
        result,
        repository=args.repository,
        flow=args.flow,
        project=args.project,
    )
    pr = github(f"/repos/{args.repository}/pulls/{data['pr_number']}")
    if not pr_is_current(pr, data):
        check = publish_check(
            github,
            args.repository,
            data,
            conclusion="neutral",
            title="Review is for an older revision",
            summary=(
                "The pull request head or base changed after this review was "
                "requested. No review was published."
            ),
        )
        return {"execution_id": execution_id, "stale": True, "check_id": check["id"]}
    review_id = None
    reported = result.get("result")
    review = reported.get("review") if isinstance(reported, dict) else None
    succeeded = data["status"] == "SUCCEEDED"
    if succeeded and isinstance(review, str) and review.strip():
        review_id = publish_review(github, args.repository, data, review)["id"]
    check = publish_check(
        github,
        args.repository,
        data,
        conclusion="success" if succeeded else "failure",
        title=f"Preloop review {data['status'].lower()}",
        summary=review if isinstance(review, str) and review else data["status"],
    )
    return {
        "execution_id": execution_id,
        "stale": False,
        "review_id": review_id,
        "check_id": check["id"],
    }


def main() -> None:
    """Public configuration from arguments; secrets only from the environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "flow", "repository"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--account")
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE_SECONDS)
    try:
        print(json.dumps(run(parser.parse_args())))
    except (VerificationError, ValueError, KeyError, TypeError):
        raise SystemExit(
            "Preloop completion verification failed; nothing was published"
        ) from None


if __name__ == "__main__":
    main()

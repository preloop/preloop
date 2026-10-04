"""Operator-run hosted CI review example. Never executes pull-request code."""

import argparse
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

TERMINAL = {"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED"}
CORRELATION = (
    "flow_id",
    "project_id",
    "repository_identifier",
    "pr_number",
    "provider_pr_id",
    "head_sha",
)


class VerificationError(Exception):
    """A safe public error, without headers, raw results or server response bodies."""


class NoCredentialRedirect(HTTPRedirectHandler):
    """Never forward credentials to a redirected endpoint."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def request_json(
    base: str, path: str, token: str, method: str = "GET", body: Any = None
) -> Any:
    """Use HTTPS and discard response/error bodies on transport failure."""
    origin = urlsplit(base)
    if (
        origin.scheme != "https"
        or not origin.netloc
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
    ):
        raise VerificationError("Use an HTTPS origin without URL credentials")
    request = Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(NoCredentialRedirect).open(request, timeout=30) as response:
            if response.geturl() != request.full_url:
                raise VerificationError("Redirected credential request rejected")
            return json.load(response)
    except HTTPError as error:
        raise VerificationError(f"Remote request failed (HTTP {error.code})") from None
    except (URLError, OSError, ValueError):
        raise VerificationError("Remote request or JSON response failed") from None


def verify_pr(pr: dict[str, Any], repository: str, head: str) -> None:
    """Accept only an open, explicitly approved same-repository exact head."""
    if (
        pr.get("state") != "open"
        or pr.get("head", {}).get("sha") != head
        or pr.get("head", {}).get("repo", {}).get("full_name") != repository
        or pr.get("base", {}).get("repo", {}).get("full_name") != repository
        or "ci-approved" not in {label.get("name") for label in pr.get("labels", [])}
    ):
        raise VerificationError(
            "PR is not an approved same-repository current exact head"
        )


def verify_execution(
    execution: dict[str, Any], expected: dict[str, Any], execution_id: str
) -> None:
    """Require every persisted correlation field, including exact SHA."""
    if execution.get("id") != execution_id or any(
        execution.get(field) != expected[field] for field in CORRELATION
    ):
        raise VerificationError("Persisted execution correlation mismatch")
    if (
        not isinstance(execution.get("provider_pr_id"), str)
        or not execution["provider_pr_id"]
    ):
        raise VerificationError("Persisted provider PR identity missing")


def verify_callback(
    raw: bytes,
    signature: str,
    secret: str,
    persisted: dict[str, Any],
    *,
    now: int,
    expected_account_id: str | None = None,
) -> dict[str, Any]:
    """Verify fresh signed bytes and their authoritative persisted completion."""
    match = re.fullmatch(r"t=([0-9]+),v1=([0-9a-f]{64})", signature)
    if match is None or abs(now - int(match[1])) > 300:
        raise VerificationError("Callback signature is invalid or stale")
    digest = hmac.new(
        secret.encode(), match[1].encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(digest, match[2]):
        raise VerificationError("Callback signature mismatch")
    try:
        envelope = json.loads(raw)
    except ValueError:
        raise VerificationError("Invalid callback JSON") from None
    if (
        not isinstance(envelope, dict)
        or set(envelope)
        != {"id", "type", "version", "occurred_at", "account_id", "data"}
        or envelope["version"] != "1"
        or envelope["type"] != "flow.execution.finished"
        or not isinstance(envelope["occurred_at"], str)
        or (
            expected_account_id is not None
            and envelope["account_id"] != expected_account_id
        )
    ):
        raise VerificationError("Callback event envelope mismatch")
    try:
        UUID(envelope["id"])
        UUID(envelope["account_id"])
    except (ValueError, TypeError, AttributeError):
        raise VerificationError("Callback event/account identity invalid") from None
    payload = envelope["data"]
    required = {
        "execution_id",
        "provider_pr_id",
        "status",
        "result_ready",
        *CORRELATION,
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise VerificationError("Callback completion schema mismatch")
    if (
        payload["execution_id"] != persisted.get("execution_id")
        or any(
            payload[field] != persisted.get(field)
            for field in (*CORRELATION, "provider_pr_id", "status")
        )
        or payload["status"] not in TERMINAL
        or payload["result_ready"] is not True
        or persisted.get("result") is None
    ):
        raise VerificationError("Callback does not match persisted completion")
    return payload


def run(args: argparse.Namespace) -> None:
    """Deduplicate, stop obsolete owned work, poll, and optionally publish proof."""
    token = os.environ.get("PRELOOP_CI_TOKEN", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not token.startswith("ci_") or not github_token:
        raise VerificationError(
            "Use a restricted CI token and a GitHub token in environment secrets"
        )
    UUID(args.project)
    UUID(args.flow)
    if args.pr <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.head):
        raise VerificationError("Use a positive PR number and exact GitHub commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise VerificationError("Use an owner/repository slug")

    def github(path: str, method: str = "GET", body: Any = None) -> Any:
        return request_json("https://api.github.com", path, github_token, method, body)

    def remote(path: str, method: str = "GET", body: Any = None) -> Any:
        return request_json(args.url, "/api/v1" + path, token, method, body)

    pr_path = f"/repos/{args.repository}/pulls/{args.pr}"
    pr = github(pr_path)
    verify_pr(pr, args.repository, args.head)
    repository_id = str(pr["base"]["repo"]["id"])
    expected = dict(
        flow_id=args.flow,
        project_id=args.project,
        repository_identifier=repository_id,
        pr_number=args.pr,
        provider_pr_id=str(pr["id"]),
        head_sha=args.head,
    )
    candidates = []
    for page in range(100):
        rows = remote(
            f"/flows/executions?flow_id={args.flow}&limit=100&skip={page * 100}"
        )
        candidates.extend(rows)
        if len(rows) < 100:
            break
    else:
        raise VerificationError("Owned history exceeds safe deduplication bound")
    execution_id = args.execution
    # Enumeration is principal-owned on the server. Correlation is still
    # checked locally before every cancellation or reuse.
    for row in candidates:
        same_pr = all(
            row.get(field) == expected[field]
            for field in CORRELATION
            if field != "head_sha"
        )
        if not same_pr:
            continue
        UUID(row["id"])
        if row.get("head_sha") == args.head and execution_id is None:
            execution_id = row["id"]
        elif row.get("head_sha") != args.head and row.get("status") not in TERMINAL:
            detail = remote(f"/flows/executions/{row['id']}")
            verify_execution(
                detail, {**expected, "head_sha": row["head_sha"]}, row["id"]
            )
            remote(
                f"/flows/executions/{row['id']}/command", "POST", {"command": "stop"}
            )
    if execution_id is None:
        created = remote(
            f"/flows/{args.flow}/trigger",
            "POST",
            {"pr_number": args.pr, "head_sha": args.head},
        )
        execution_id = created["id"]
        verify_execution(created, expected, execution_id)
    UUID(execution_id)
    deadline = time.monotonic() + args.timeout
    while True:
        detail = remote(f"/flows/executions/{execution_id}")
        verify_execution(detail, expected, execution_id)
        try:
            verify_pr(github(pr_path), args.repository, args.head)
        except VerificationError:
            remote(
                f"/flows/executions/{execution_id}/command", "POST", {"command": "stop"}
            )
            raise
        if detail["status"] in TERMINAL:
            break
        if time.monotonic() >= deadline:
            remote(
                f"/flows/executions/{execution_id}/command", "POST", {"command": "stop"}
            )
            raise VerificationError("Timed out; owned execution stopped")
        time.sleep(10)
    result = remote(f"/flows/executions/{execution_id}/result")
    verify_execution(result, expected, execution_id)
    if (
        result.get("execution_id") != execution_id
        or result.get("status") != detail["status"]
    ):
        raise VerificationError(
            "Result does not match authoritative terminal execution"
        )
    if args.callback_body:
        from pathlib import Path

        secret = os.environ.get("PRELOOP_CALLBACK_SECRET", "")
        signature = os.environ.get("PRELOOP_CALLBACK_SIGNATURE", "")
        if not secret or not signature:
            raise VerificationError(
                "Callback verification requires signing secret and original signature"
            )
        verify_callback(
            Path(args.callback_body).read_bytes(),
            signature,
            secret,
            result,
            now=int(time.time()),
            expected_account_id=getattr(args, "callback_account", None),
        )
    publication_id = None
    if args.publish_review:
        if detail["status"] != "SUCCEEDED":
            raise VerificationError(
                "Only a successful matching result may publish a review"
            )
        reported = result.get("result")
        review = reported.get("review") if isinstance(reported, dict) else None
        if not isinstance(review, str) or not review.strip():
            raise VerificationError("Persisted result has no review text")
        marker = f"<!-- preloop-ci:{execution_id}:{args.head} -->"
        # GITHUB_TOKEN is an installation token and cannot call GET /user.
        # Resolve the expected public author and require it on the receipt.
        author_id = github("/users/" + quote(args.review_author, safe=""))["id"]
        existing = []
        for page in range(1, 101):
            reviews = github(pr_path + f"/reviews?per_page=100&page={page}")
            existing.extend(reviews)
            if len(reviews) < 100:
                break
        else:
            raise VerificationError("Review history exceeds safe deduplication bound")
        found = next(
            (
                item
                for item in existing
                if marker in item.get("body", "")
                and item.get("commit_id") == args.head
                and item.get("user", {}).get("id") == author_id
                and item.get("state") == "COMMENTED"
                and isinstance(item.get("submitted_at"), str)
                and item["submitted_at"]
            ),
            None,
        )
        verify_pr(github(pr_path), args.repository, args.head)
        posted = found or github(
            pr_path + "/reviews",
            "POST",
            {
                "commit_id": args.head,
                "event": "COMMENT",
                "body": review[:60000] + "\n\n" + marker,
            },
        )
        proof = github(pr_path + f"/reviews/{posted['id']}")
        if (
            proof.get("commit_id") != args.head
            or marker not in proof.get("body", "")
            or proof.get("user", {}).get("id") != author_id
            or proof.get("state") != "COMMENTED"
            or not isinstance(proof.get("submitted_at"), str)
            or not proof["submitted_at"]
        ):
            raise VerificationError("Review publication receipt mismatch")
        publication_id = proof["id"]
    verify_pr(github(pr_path), args.repository, args.head)
    print(
        json.dumps(
            {
                "execution_id": execution_id,
                "head_sha": args.head,
                "status": detail["status"],
                "review_id": publication_id,
            }
        )
    )


def main() -> None:
    """Read only public configuration from arguments; secrets stay in environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("url", "project", "flow", "repository", "head"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--execution")
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--callback-body")
    parser.add_argument("--callback-account")
    parser.add_argument("--publish-review", action="store_true")
    parser.add_argument("--review-author", default="github-actions[bot]")
    try:
        run(parser.parse_args())
    except (VerificationError, ValueError, KeyError, TypeError):
        raise SystemExit(
            "Restricted CI verification failed; no unverified review was published"
        ) from None


if __name__ == "__main__":
    main()

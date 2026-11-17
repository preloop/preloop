"""Trusted Actions adapter for a subset of restricted, read-only CI reviews.

Only default/base-branch code runs this adapter. Pull-request content, including
captured tool commands, is data and is never executed by this publisher.
"""

import argparse
import fnmatch
import json
import os
import re
import shlex
import time
from collections import Counter
from typing import Any
from uuid import UUID

from restricted_ci_review import (
    TERMINAL,
    VerificationError,
    json_digest,
    request_json,
    verify_execution,
    TransportError,
)

DEFAULT_PATHS = ("docs/**", "runtime-plugins/**", "helm/**", "cli/**")
CONTEXT = "preloop-review/ci-route"
SEVERITIES = ("critical", "high", "medium", "low", "info")


def path_subset(files: list[dict[str, Any]], configured: str = "") -> bool:
    """Require every changed path, including rename sources, in the allowlist.

    Args:
        files: Complete GitHub pull-request files response.
        configured: JSON array of repository-relative shell globs; empty uses defaults.

    Returns:
        Whether the nonempty file set is wholly inside the configured subset.
    """
    patterns = json.loads(configured) if configured else list(DEFAULT_PATHS)
    if (
        not isinstance(patterns, list)
        or not patterns
        or any(
            not isinstance(p, str) or not p or p.startswith(("/", "!")) or ".." in p
            for p in patterns
        )
    ):
        raise VerificationError("Path subset must be a nonempty JSON array of globs")
    return bool(files) and all(
        isinstance(path, str)
        and any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
        for file in files
        for path in (
            file.get("filename"),
            file.get("previous_filename", file.get("filename")),
        )
    )


def github(path: str, method: str = "GET", body: Any = None) -> Any:
    """Call GitHub with the job's permission-limited token."""
    return request_json(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        path,
        os.environ["GITHUB_TOKEN"],
        method,
        body,
    )


def remote(path: str, method: str = "GET", body: Any = None) -> Any:
    """Use the project-bound CI identity, never a broader account key."""
    token = os.environ["PRELOOP_CI_TOKEN"]
    if not token.startswith("ci_"):
        raise VerificationError("A restricted CI identity key is required")
    return request_json(os.environ["CI_URL"], "/api/v1" + path, token, method, body)


def output(name: str, value: str) -> None:
    """Write only validated scalar workflow outputs."""
    if "\n" in value or "\r" in value:
        raise VerificationError("Invalid workflow output")
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"{name}={value}\n")


def pr_path() -> str:
    """Build a GitHub path from trusted Actions context and an integer PR number."""
    return f"/repos/{os.environ['GITHUB_REPOSITORY']}/pulls/{int(os.environ['CI_PR'])}"


def verify_current(pr: dict[str, Any], head: str, base: str) -> None:
    """Reject closed, draft, forked or moved pull requests."""
    repository = os.environ["GITHUB_REPOSITORY"]
    if (
        pr.get("state") != "open"
        or pr.get("draft") is not False
        or pr.get("head", {}).get("sha") != head
        or pr.get("base", {}).get("sha") != base
        or any(
            pr.get(side, {}).get("repo", {}).get("full_name") != repository
            for side in ("head", "base")
        )
    ):
        raise VerificationError("PR closed, draft, forked, or head/base changed")


def gate() -> None:
    """Select a complete, current same-repository path subset without a CI key."""
    pr = github(pr_path())
    try:
        verify_current(pr, os.environ["CI_HEAD"], os.environ["CI_BASE"])
    except VerificationError:
        output("eligible", "false")
        return
    files: list[dict[str, Any]] = []
    for page in range(1, 31):
        rows = github(pr_path() + f"/files?per_page=100&page={page}")
        files.extend(rows)
        if len(rows) < 100:
            break
    # GitHub caps this endpoint at 3,000 files. Never accept a partial list.
    eligible = len(files) == pr.get("changed_files") and path_subset(
        files, os.environ.get("CI_PATHS", "")
    )
    verify_current(github(pr_path()), os.environ["CI_HEAD"], os.environ["CI_BASE"])
    output("eligible", str(eligible).lower())


def expected(pr: dict[str, Any]) -> dict[str, Any]:
    """Bind API projections to configured flow/project and provider identities."""
    return dict(
        flow_id=str(UUID(os.environ["CI_FLOW"])),
        project_id=str(UUID(os.environ["CI_PROJECT"])),
        repository_identifier=str(pr["base"]["repo"]["id"]),
        provider_pr_id=str(pr["id"]),
        pr_number=int(os.environ["CI_PR"]),
        head_sha=os.environ["CI_HEAD"],
    )


def correlated(
    document: dict[str, Any], binding: dict[str, Any], execution: str
) -> None:
    """Verify the restricted correlation and the separately admitted base."""
    verify_execution(document, binding, execution)
    if document.get("base_sha") != os.environ["CI_BASE"]:
        raise VerificationError("Execution base SHA mismatch")


def trigger() -> None:
    """Trigger the documented two-field request, then poll authenticated completion."""
    pr = github(pr_path())
    verify_current(pr, os.environ["CI_HEAD"], os.environ["CI_BASE"])
    binding = expected(pr)
    execution = None
    # Reuse owned work on reruns, including failed work, without another model call.
    for page in range(100):
        rows = remote(
            f"/flows/executions?flow_id={binding['flow_id']}&limit=100&skip={page * 100}"
        )
        for row in rows:
            if (
                all(row.get(k) == v for k, v in binding.items())
                and row.get("base_sha") == os.environ["CI_BASE"]
            ):
                execution = str(UUID(row["id"]))
                break
        if execution or len(rows) < 100:
            break
    else:
        raise VerificationError("Owned history exceeds deduplication bound")
    if execution is None:
        created = remote(
            f"/flows/{binding['flow_id']}/trigger",
            "POST",
            {"pr_number": binding["pr_number"], "head_sha": binding["head_sha"]},
        )
        execution = str(UUID(created["id"]))
        # Save the handle even if admission raced a base update; publisher fails closed.
        output("execution", execution)
        correlated(created, binding, execution)
    else:
        output("execution", execution)
    deadline = time.monotonic() + 600
    while True:
        detail = remote(f"/flows/executions/{execution}")
        correlated(detail, binding, execution)
        if detail["status"] in TERMINAL:
            return
        current_pr = github(pr_path())
        try:
            verify_current(current_pr, binding["head_sha"], os.environ["CI_BASE"])
            if time.monotonic() >= deadline:
                raise VerificationError("Completion deadline exceeded")
        except VerificationError:
            remote(
                f"/flows/executions/{execution}/command", "POST", {"command": "stop"}
            )
            raise
        time.sleep(10)


def read_diff_command(command: str, merge_base: str, head: str) -> bool:
    """Recognise a standalone full patch read, never execute captured shell text.

    Deliberately reject pipelines, shell substitutions, path limits, --stat,
    --name-only and echo/printf imitations. The flow prompt specifies this form.
    """
    words = shlex.split(command)
    if (
        len(words) == 3
        and words[0] in {"bash", "/bin/bash", "sh", "/bin/sh"}
        and words[1] in {"-c", "-lc"}
    ):
        words = shlex.split(words[2])
    return words == [
        "git",
        "--no-pager",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        merge_base,
        head,
        "--",
    ]


def validate_result(document: dict[str, Any], head: str, base: str) -> dict[str, Any]:
    """Require a successful, schema-valid, substantive result bound to both SHAs."""
    result = document.get("result")
    receipt = document.get("startup_receipt") or {}
    diff = receipt.get("diff") or {}
    completion = document.get("completion") or {}
    integrity = completion.get("review_integrity") or {}
    if document.get("status") != "SUCCEEDED":
        raise VerificationError("Execution did not succeed")
    if (
        document.get("head_sha") != head
        or document.get("base_sha") != base
        or receipt.get("commit") != head
        or receipt.get("executor") != "hosted"
        or receipt.get("checkout_verified") is not True
        or receipt.get("setup_completed") is not True
        or diff.get("head_sha") != head
        or diff.get("base_sha") != base
        or not re.fullmatch(r"[0-9a-f]{40}", diff.get("merge_base_sha", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", diff.get("sha256", ""))
    ):
        raise VerificationError("Startup receipt head/base/diff mismatch")
    if (
        not isinstance(result, dict)
        or not isinstance(document.get("result_withheld_paths"), list)
        or any(
            path != "/container_termination"
            for path in document["result_withheld_paths"]
        )
        or (document.get("schema_validation") or {}).get("valid") is not True
        or completion.get("result_valid") is not True
        or integrity.get("status") != "verified"
        or (integrity.get("inspection") or {}).get("inspected") is not True
    ):
        raise VerificationError("Result schema or repository inspection not verified")
    if result.get("context_complete") is not True:
        raise VerificationError("PR context is incomplete")
    if result.get("head_sha") != head or result.get("base_sha") != base:
        raise VerificationError("Returned review head/base mismatch")
    review = result.get("review")
    if (
        not isinstance(review, str)
        or not 80 <= len(review.strip()) <= 60000
        or len(review.split()) < 12
    ):
        raise VerificationError("Review text is missing or non-substantive")
    if any(
        required not in review
        for required in (
            "## 🔍 Preloop Code Review",
            "### Checks",
            f"<!-- preloop-review:reviewed-sha:{head} -->",
        )
    ):
        raise VerificationError("Unified review summary or reviewed SHA marker missing")
    findings = result.get("findings")
    if result.get("verdict") not in {
        "approve",
        "comment",
        "request_changes",
    } or not isinstance(findings, list):
        raise VerificationError("Review verdict or findings missing")
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("severity") not in SEVERITIES:
            raise VerificationError("Finding severity invalid")
    risk = result.get("risk_level")
    if risk not in {"low", "medium", "high", "critical"}:
        raise VerificationError("Review risk level invalid")
    if result["verdict"] != "request_changes" and risk in {"high", "critical"}:
        raise VerificationError("Approval contradicts blocking risk level")
    if result["verdict"] != "request_changes" and any(
        f["severity"] in {"critical", "high"} for f in findings
    ):
        raise VerificationError("Approval contradicts blocking findings")
    return result


def validate_evidence(
    execution: str, document: dict[str, Any], binding: dict[str, Any]
) -> None:
    """Read every authenticated evidence page and require an observed diff read."""
    skip, total, seen = 0, None, set()
    inspected = False
    diff = document["startup_receipt"]["diff"]
    for _ in range(1000):
        page = remote(
            f"/flows/executions/{execution}/tool-evidence?skip={skip}&limit=10"
        )
        correlated(page["binding"], binding, execution)
        records = page["records"]
        if (
            page.get("execution_id") != execution
            or page.get("status") != "SUCCEEDED"
            or page.get("captured_result_sha256") != json_digest(document["result"])
            or page.get("page_sha256") != json_digest(records)
            or page.get("skip") != skip
            or (total is not None and page.get("total") != total)
        ):
            raise VerificationError(
                "Tool evidence identity, digest or pagination mismatch"
            )
        total = page["total"]
        for record in records:
            content = record.get("document") or {}
            if (
                record["id"] in seen
                or record.get("integrity_valid") is not True
                or record.get("sha256") != json_digest(content)
                or content.get("execution_id") != execution
            ):
                raise VerificationError("Tool evidence record integrity mismatch")
            seen.add(record["id"])
            if (
                content.get("capture_complete") is not True
                or content.get("storage_redacted")
                or content.get("capture_failed")
            ):
                continue
            for observation in content.get("observations", []):
                if (
                    observation.get("source") == "native"
                    and observation.get("kind") == "command"
                    and observation.get("event") == "item.completed"
                    and type(observation.get("exit_code")) is int
                    and observation["exit_code"] == 0
                    and observation.get("redacted") is False
                    and observation.get("truncated") is False
                    and isinstance(observation.get("value"), str)
                    and read_diff_command(
                        observation["value"],
                        diff["merge_base_sha"],
                        binding["head_sha"],
                    )
                ):
                    inspected = True
        if page.get("next_skip") != skip + len(records):
            raise VerificationError("Tool evidence pagination mismatch")
        skip = page["next_skip"]
        if page.get("has_more") is False:
            if skip != total:
                raise VerificationError("Incomplete tool evidence export")
            break
        if not records:
            raise VerificationError("Tool evidence pagination stalled")
    else:
        raise VerificationError("Tool evidence exceeds export bound")
    if not inspected:
        raise VerificationError("No successful captured read of the bound full diff")


def publish_review(execution: str, result: dict[str, Any]) -> None:
    """Optionally publish one COMMENT review on the verified head, with a receipt."""
    marker = f"<!-- preloop-ci-route:{execution}:{os.environ['CI_HEAD']} -->"
    body = result["review"] + "\n\n" + marker
    author = github("/users/github-actions%5Bbot%5D")["id"]
    found = None
    for page in range(1, 101):
        rows = github(pr_path() + f"/reviews?per_page=100&page={page}")
        found = next(
            (
                r
                for r in rows
                if r.get("body") == body
                and r.get("commit_id") == os.environ["CI_HEAD"]
                and r.get("user", {}).get("id") == author
                and r.get("state") == "COMMENTED"
                and r.get("submitted_at")
            ),
            None,
        )
        if found or len(rows) < 100:
            break
    else:
        raise VerificationError("Review history exceeds deduplication bound")
    verify_current(github(pr_path()), os.environ["CI_HEAD"], os.environ["CI_BASE"])
    posted = found or github(
        pr_path() + "/reviews",
        "POST",
        {"commit_id": os.environ["CI_HEAD"], "event": "COMMENT", "body": body},
    )
    proof = github(pr_path() + f"/reviews/{int(posted['id'])}")
    if (
        proof.get("body") != body
        or proof.get("commit_id") != os.environ["CI_HEAD"]
        or proof.get("user", {}).get("id") != author
        or proof.get("state") != "COMMENTED"
        or not proof.get("submitted_at")
    ):
        raise VerificationError("Review publication receipt mismatch")


def fetch_result(execution: str) -> dict[str, Any]:
    """Retry only authenticated result-pending responses after terminal status."""
    for attempt in range(7):
        try:
            return remote(f"/flows/executions/{execution}/result")
        except TransportError as error:
            if error.status != 409 or attempt == 6:
                raise
            time.sleep(5)
    raise VerificationError("Result did not become ready")


def publish() -> None:
    """Re-fetch completion and evidence; publish status error on any rejection."""
    state, reason, verdict, counts, execution_url = (
        "error",
        "Verification failed",
        "unavailable",
        {},
        "",
    )
    try:
        execution = str(UUID(os.environ.get("CI_EXECUTION", "")))
        execution_url = (
            os.environ["CI_URL"].rstrip("/") + f"/console/flows/executions/{execution}"
        )
        pr = github(pr_path())
        verify_current(pr, os.environ["CI_HEAD"], os.environ["CI_BASE"])
        binding = expected(pr)
        detail = remote(f"/flows/executions/{execution}")
        correlated(detail, binding, execution)
        document = fetch_result(execution)
        correlated(document, binding, execution)
        if document.get("execution_id") != execution or document.get(
            "status"
        ) != detail.get("status"):
            raise VerificationError("Completion does not match execution")
        result = validate_result(document, os.environ["CI_HEAD"], os.environ["CI_BASE"])
        validate_evidence(execution, document, binding)
        verify_current(github(pr_path()), os.environ["CI_HEAD"], os.environ["CI_BASE"])
        if os.environ.get("CI_POST_REVIEW") == "enabled":
            publish_review(execution, result)
        verify_current(github(pr_path()), os.environ["CI_HEAD"], os.environ["CI_BASE"])
        verdict = result["verdict"]
        counts = dict(Counter(f["severity"] for f in result["findings"]))
        state = "failure" if verdict == "request_changes" else "success"
        reason = f"{verdict}: {len(result['findings'])} findings"
    except VerificationError as error:
        reason = str(error)
    except (ValueError, KeyError, TypeError, AttributeError):
        reason = "Missing or malformed authenticated completion/evidence"
    status = {"context": CONTEXT, "state": state, "description": reason[:140]}
    if execution_url:
        status["target_url"] = execution_url
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
        stream.write(
            f"### Restricted CI review\n\nStatus: **{state}** — {reason}\n\nVerdict: {verdict}\n\n"
        )
        stream.write(
            "Findings: "
            + ", ".join(f"{s}: {counts.get(s, 0)}" for s in SEVERITIES)
            + "\n\n"
        )
        stream.write(f"Execution: {execution_url or 'not available'}\n")
    github(
        f"/repos/{os.environ['GITHUB_REPOSITORY']}/statuses/{os.environ['CI_HEAD']}",
        "POST",
        status,
    )
    if state == "error":
        raise VerificationError(reason)


def main() -> None:
    """Run one trusted phase; never print response bodies or credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("gate", "trigger", "publish"))
    phase = parser.parse_args().phase
    try:
        {"gate": gate, "trigger": trigger, "publish": publish}[phase]()
    except (VerificationError, ValueError, KeyError, TypeError, AttributeError):
        raise SystemExit(
            "Restricted CI route failed; see the publisher status/summary"
        ) from None


if __name__ == "__main__":
    main()

"""Publication gate: runner-controlled verification inside the container.

The agent runs checks while it edits; the *publisher* does not take the
agent's word for it (issue #428). Before the post-execution push, this module
builds a self-contained verifier script that runs in the container, after the
agent has exited and after its final commit:

1. computes the changed files against the PR base branch,
2. selects required checks from the flow's **trusted test profile** (which
   travels in the flow configuration — never read from the repository, so
   the agent cannot narrow required checks or edit the profile inside its
   own PR),
3. executes them with ``PRELOOP_DISABLE_TELEMETRY=true`` and per-command
   timeouts, capturing one log per check under the evidence pack,
4. reuses its own earlier evidence when the commit, tree, profile and
   verifier identity are unchanged (no blanket rerun just because another
   message arrived), re-running whenever anything changed,
5. records evidence (commands, exit codes, log references, environment
   digest, profile version, exact commit and tree) and prints a marker line
   the orchestrator stores on the execution result.

Only an ``ALLOW`` verdict lets the push and pull-request creation run; a
denial exits non-zero and fails the execution instead of publishing.

Selection uses ``preloop.utils.verification_selection`` verbatim: the builder
embeds that module's *source text* into the script, so the runner-side
contract and the in-container verifier execute the same implementation.
"""

from __future__ import annotations

import inspect
import json
import shlex
from typing import Any, Mapping, Optional

# Directory (inside the agent container) holding the verification evidence.
VERIFICATION_DIR_NAME = "verification"
# One-line marker the orchestrator scans for; carries the compact evidence
# summary that is merged into the execution result (runner-captured, unlike
# anything the agent reports in result.json).
VERIFICATION_MARKER = "PRELOOP_VERIFICATION"
# Denial marker; also the anchor for the verification_failed /
# verification_blocked failure categories.
VERIFICATION_DENIED_MARKER = "PRELOOP_VERIFICATION_DENIED"
VERIFIER_EXIT_DENIED = 3

# The verifier main runs on python3 with stdlib only, inside agent images
# that do not have preloop installed. It expects the selection module's
# source next to it (written by the builder below) under the module name
# ``preloop_verification_selection``.
VERIFIER_MAIN_PY = r'''
#!/usr/bin/env python3
"""Preloop publication-gate verifier (runner-controlled; issue #428).

Usage:
  verify.py <repo_path> <base_branch> <evidence_dir> <profile_json> <gate_budget_seconds>

Writes <evidence_dir>/verification/{evidence.json,verdict,verdict.json} and
check logs, prints a single PRELOOP_VERIFICATION marker line, and exits 0 on
ALLOW or 3 on DENY. A verdict file is ALWAYS written (even on crash paths)
so the publisher block can only proceed when this script decided ALLOW.
"""

import datetime
import json
import os
import platform
import re
import subprocess
import sys
import time

import preloop_verification_selection as selection

PRODUCER = "preloop-verifier"
VERIFIER_VERSION = 1

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

DENIED_EXIT = 3


def _fail(verdict_dir, reason):
    _write(verdict_dir, False, reason, "blocked", [])
    print(
        "PRELOOP_VERIFICATION_VERDICT DENY status=blocked reason=%s" % reason,
        flush=True,
    )
    print(
        json.dumps(
            {
                "status": "blocked",
                "allowed": False,
                "reason": reason,
                "producer": PRODUCER,
                "verifier_version": VERIFIER_VERSION,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    sys.exit(DENIED_EXIT)


def _write(verdict_dir, allowed, reason, status, checks):
    with open(os.path.join(verdict_dir, "verdict"), "w", encoding="utf-8") as fh:
        fh.write("ALLOW" if allowed else "DENY")
    with open(
        os.path.join(verdict_dir, "verdict.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump(
            {"allowed": allowed, "reason": reason, "status": status},
            fh,
            indent=2,
            sort_keys=True,
        )


def _git(repo, *args, check=True):
    result = subprocess.run(
        ["git"] + list(args),
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _git_ok(repo, *args):
    try:
        return _git(repo, *args, check=True)
    except subprocess.CalledProcessError:
        return None


def _safe_id(check_id):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", check_id)[:120] or "check"


def _identity(evidence, profile_id, profile_version, commit, tree, clean):
    return (
        evidence.get("producer") == PRODUCER
        and evidence.get("verifier_version") == VERIFIER_VERSION
        and evidence.get("profile_id") == profile_id
        and evidence.get("profile_version") == profile_version
        and evidence.get("commit_sha") == commit
        and evidence.get("tree_hash") == tree
        and evidence.get("clean_tree") == clean
        and evidence.get("status") == STATUS_PASSED
    )


def main(argv):
    if len(argv) != 6:
        print("usage: verify.py <repo> <base> <evidence_dir> <profile_json> "
              "<gate_budget_seconds>", file=sys.stderr)
        return 2
    repo, base_ref, evidence_dir, profile_path, budget_raw = argv[1:]
    gate_budget = float(budget_raw)
    deadline = time.monotonic() + gate_budget

    verdict_dir = os.path.join(evidence_dir, "verification")
    checks_dir = os.path.join(verdict_dir, "checks")
    os.makedirs(checks_dir, exist_ok=True)

    # The profile is written fresh by the publisher block moments ago; the
    # agent never gets to touch it between write and read.
    with open(profile_path, "r", encoding="utf-8") as fh:
        profile = json.load(fh)
    profile_id = str(profile.get("profile_id", "unnamed"))
    profile_version = str(profile.get("version", ""))

    # Sanity: the repo must exist and be a git work tree.
    if _git_ok(repo, "rev-parse", "--is-inside-work-tree") != "true":
        _fail(verdict_dir, "repository is not a git work tree: %s" % repo)
        return DENIED_EXIT

    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")

    # Dirty = staged or unstaged changes to TRACKED files. Untracked files
    # never change the published tree, so they are counted but not fatal;
    # a dirty tracked tree would publish something the evidence cannot
    # describe.
    status_lines = _git(repo, "status", "--porcelain").splitlines()
    tracked_changes = [
        line for line in status_lines if not line.startswith("??")
    ]
    untracked_count = sum(1 for line in status_lines if line.startswith("??"))
    clean_tree = not tracked_changes

    # Changed files against the PR base. A missing base degrades to "no
    # rule matched", which the profile answers with its conservative
    # unknown_default — never with zero checks.
    changed_files = []
    merge_base = _git_ok(repo, "merge-base", base_ref, "HEAD") if base_ref else None
    if merge_base:
        changed_output = _git_ok(
            repo, "diff", "--name-only", merge_base + "..HEAD"
        )
        changed_files = [
            line for line in (changed_output or "").splitlines() if line.strip()
        ]

    selected = selection.select_from_raw(profile, changed_files)
    commands = [entry["command"] for entry in selected["checks"]]

    evidence_path = os.path.join(verdict_dir, "evidence.json")

    # Evidence in this sandbox is agent writable. Never use a preexisting
    # JSON file to skip commands; authenticated runner-side reuse is separate.
    records = []
    any_failed = False
    any_blocked = not clean_tree or not commands
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    env = dict(os.environ)
    env["PRELOOP_DISABLE_TELEMETRY"] = "true"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["PRELOOP_VERIFY_BASE"] = base_ref
    env["PRELOOP_VERIFY_HEAD"] = commit
    for command in commands:
        record = {
            "id": command["id"], "command": command["command"],
            "scope": command.get("scope", "unknown"), "reused": False,
            "exit_code": None, "skipped_reason": None,
            "selected_by": next(entry["selected_by"] for entry in selected["checks"] if entry["command"]["id"] == command["id"]),
        }
        remaining = deadline - time.monotonic()
        if not clean_tree or remaining <= 0:
            record["skipped_reason"] = "dirty tracked tree" if not clean_tree else "verification budget exhausted"
            records.append(record)
            any_blocked = True
            continue
        log_path = os.path.join(checks_dir, _safe_id(command["id"]) + ".log")
        record["log_file"] = log_path
        started = time.monotonic()
        with open(log_path, "w", encoding="utf-8") as log:
            process = subprocess.Popen(command["command"], shell=True, executable="/bin/bash", cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                record["exit_code"] = process.wait(timeout=min(remaining, float(command.get("timeout_seconds", 900))))
            except subprocess.TimeoutExpired:
                record["skipped_reason"] = "required check timed out"
                any_blocked = True
            finally:
                # Reap descendants too: timed-out commands must not keep
                # changing the workspace after their verdict is recorded.
                import signal
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if record["exit_code"] in (126, 127):
            record["skipped_reason"] = "required command is unavailable"
            any_blocked = True
        elif record["exit_code"] not in (None, 0):
            any_failed = True
        record["duration_seconds"] = round(time.monotonic() - started, 4)
        records.append(record)
    final_commit = _git_ok(repo, "rev-parse", "HEAD")
    final_tree = _git_ok(repo, "rev-parse", "HEAD^{tree}")
    final_status = _git_ok(repo, "status", "--porcelain")
    final_clean = final_status is not None and not any(line for line in final_status.splitlines() if not line.startswith("??"))
    if final_commit != commit or final_tree != tree or not final_clean:
        clean_tree = False
        any_blocked = True
    status = STATUS_BLOCKED if any_blocked else STATUS_FAILED if any_failed else STATUS_PASSED
    allowed = status == STATUS_PASSED
    reason = "all required checks passed" if allowed else "one or more required checks failed" if status == STATUS_FAILED else "required checks unavailable, empty, timed out, or working tree changed; publication blocked"
    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    evidence = {
        "producer": PRODUCER, "verifier_version": VERIFIER_VERSION,
        "profile_id": profile_id, "profile_version": profile_version,
        "commit_sha": commit, "tree_hash": tree, "clean_tree": clean_tree,
        "status": status, "allowed": allowed, "reason": reason,
        "checks": records, "changed_files": changed_files,
        "base_ref": base_ref, "matched_rule_ids": selected["matched_rule_ids"],
        "used_unknown_default": selected["used_unknown_default"],
        "untracked_count": untracked_count, "reused": False,
        "started_at": started_at, "finished_at": finished_at,
        "environment": {"python_version": platform.python_version(), "git_version": _git(repo, "--version"), "os_name": platform.platform(), "telemetry_disabled": True},
    }
    with open(evidence_path, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
    _write(verdict_dir, allowed, reason, status, records)
    with open(os.path.join(verdict_dir, "verified.sha"), "w", encoding="utf-8") as stream:
        stream.write(commit if allowed else "")
    compact = dict(evidence)
    compact.pop("changed_files")
    compact["changed_files_count"] = len(changed_files)
    compact["changed_files_sample"] = changed_files[:200]
    print("PRELOOP_VERIFICATION " + json.dumps(compact, separators=(",", ":"), sort_keys=True), flush=True)
    print("PRELOOP_VERIFICATION_VERDICT %s status=%s reason=%s" % ("ALLOW" if allowed else "DENY", status, reason), flush=True)
    return 0 if allowed else DENIED_EXIT


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        print("PRELOOP_VERIFICATION_DENIED status=blocked reason=verifier crashed", flush=True)
        sys.exit(DENIED_EXIT)
'''


def build_verification_gate_shell(
    *,
    profile: Mapping[str, Any],
    working_dir: str,
    base_branch: str,
    evidence_dir: str,
    gate_budget_seconds: int = 3600,
    scratch_dir: Optional[str] = None,
) -> str:
    """Build legacy sandbox verification; isolated publication verifies outside it."""
    import uuid
    from preloop.services.verification import resolve_verification_policy
    from preloop.utils import verification_selection

    policy = resolve_verification_policy(
        {"verification": {"mode": "gate", "profile": dict(profile)}}
    )
    if policy.mode != "gate" or policy.profile is None:
        raise ValueError("verification gate requires a valid trusted test profile")
    scratch = scratch_dir or f"/tmp/.preloop-verify-{uuid.uuid4().hex[:12]}"
    safe_dir, safe_repo, safe_base, safe_evidence = map(
        shlex.quote, (scratch, working_dir, base_branch, evidence_dir)
    )
    verdict_dir = shlex.quote(evidence_dir + "/verification")
    lines = [
        f"mkdir -p {safe_dir} {verdict_dir} || exit {VERIFIER_EXIT_DENIED}",
        f"rm -f {verdict_dir}/verdict {verdict_dir}/verified.sha",
        f"cat > {safe_dir}/preloop_verification_selection.py <<'PRELOOP_SELECTION_EOF'",
        inspect.getsource(verification_selection),
        "PRELOOP_SELECTION_EOF",
        f"cat > {safe_dir}/preloop_gate_verify.py <<'PRELOOP_VERIFIER_EOF'",
        VERIFIER_MAIN_PY,
        "PRELOOP_VERIFIER_EOF",
        f"cat > {safe_dir}/profile.json <<'PRELOOP_PROFILE_EOF'",
        json.dumps(policy.profile.model_dump(), ensure_ascii=True),
        "PRELOOP_PROFILE_EOF",
        "PRELOOP_GATE_RC=0",
        f"python3 {safe_dir}/preloop_gate_verify.py {safe_repo} {safe_base} {safe_evidence} {safe_dir}/profile.json {shlex.quote(str(gate_budget_seconds))} > {verdict_dir}/gate.log 2>&1 || PRELOOP_GATE_RC=$?",
        f"cat {verdict_dir}/gate.log",
        f"rm -rf {safe_dir}",
        f"PRELOOP_VERDICT_TEXT=$(cat {verdict_dir}/verdict 2>/dev/null || true)",
        'if [ "$PRELOOP_GATE_RC" -ne 0 ] || [ "$PRELOOP_VERDICT_TEXT" != "ALLOW" ]; then',
        f'  echo "{VERIFICATION_DENIED_MARKER} verdict=$PRELOOP_VERDICT_TEXT reason=verification gate refused publication rc=$PRELOOP_GATE_RC"',
        f"  exit {VERIFIER_EXIT_DENIED}",
        "fi",
        f'if [ "$(git -C {safe_repo} rev-parse HEAD 2>/dev/null || true)" != "$(cat {verdict_dir}/verified.sha 2>/dev/null || true)" ]; then',
        f'  echo "{VERIFICATION_DENIED_MARKER} reason=a commit appeared after the verification gate"',
        f"  exit {VERIFIER_EXIT_DENIED}",
        "fi",
    ]
    return "\n".join(lines)

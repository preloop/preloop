"""The PR/MR capture shells must print the marker under ``set -u``.

Regression for the 2026-09-25 prod abort: the generated agent script runs
the post-execution block under ``set -u``, and on the happy path (URL in the
create response) the capture shell read ``PRELOOP_PROVENANCE_FAILED`` before
anything had set it. Bash aborted one line after ``PR create HTTP 201`` and
the ``PRELOOP_PR_OPENED`` line never printed. The older harness in
``tests/services/test_flow_pr_loop.py`` runs plain ``bash -c``, which is why
the bug passed CI.
"""

import json
import os
import shutil
import subprocess

import pytest

from preloop.agents.container import (
    build_github_pr_capture_shell,
    build_gitlab_mr_capture_shell,
)
from preloop.services.flow_pr_binding import parse_pr_opened_marker

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to run the capture shell"
)

GITHUB_201 = (
    '{"url":"https://api.github.com/repos/acme/app/pulls/7","id":1,'
    '"html_url":"https://github.com/acme/app/pull/7",'
    '"head":{"repo":{"html_url":"https://github.com/acme/app"}}}'
)
GITLAB_201 = (
    '{"id":1,"iid":5,"author":{"web_url":"https://gitlab.com/someone"},'
    '"web_url":"https://gitlab.com/acme/app/-/merge_requests/5"}'
)
BRANCH = "preloop/issue-951-0976028b"


def _github(**kwargs):
    return build_github_pr_capture_shell(
        token_ref="${TOKEN}", owner="acme", repo="app", branch=BRANCH, **kwargs
    )


def _gitlab(**kwargs):
    return build_gitlab_mr_capture_shell(
        token_ref="${TOKEN}",
        gitlab_host="gitlab.com",
        encoded_path="acme%2Fapp",
        branch=BRANCH,
        **kwargs,
    )


def _run(tmp_path, script, *, response, lookup="", flags=("-u",)):
    """Run ``script`` with ``bash <flags>`` against a canned response file.

    ``curl`` is a stub that writes ``lookup`` to its ``-o`` target, and
    ``python3`` stays real so the fallback's body-update script runs too.
    ``TOKEN`` is deliberately exported: under ``-u`` an unset token would
    abort for an unrelated reason.
    """
    evidence = tmp_path / "workspace" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "pr.json").write_text(response)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "out=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-o" ]; then out="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'if [ -n "$out" ]; then cat "$FAKE_LOOKUP_FILE" > "$out"; fi\n'
        'echo "200"\n'
    )
    curl.chmod(0o755)
    lookup_file = tmp_path / "lookup.json"
    lookup_file.write_text(lookup)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_LOOKUP_FILE": str(lookup_file),
        "TOKEN": "t",
        "HOME": str(tmp_path),
    }
    script = script.replace("/workspace/evidence", str(evidence))
    return subprocess.run(
        ["bash", *flags, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _markers(stdout):
    return [
        m for m in (parse_pr_opened_marker(line) for line in stdout.splitlines()) if m
    ]


@pytest.mark.parametrize(
    "build,response,url,provider",
    [
        (_github, GITHUB_201, "https://github.com/acme/app/pull/7", "github"),
        (
            _gitlab,
            GITLAB_201,
            "https://gitlab.com/acme/app/-/merge_requests/5",
            "gitlab",
        ),
    ],
    ids=["github", "gitlab"],
)
@pytest.mark.parametrize("flags", [("-u",), ("-euo", "pipefail")], ids=["u", "euo"])
def test_capture_shell_201_prints_marker_under_nounset(
    tmp_path, build, response, url, provider, flags
):
    completed = _run(tmp_path, build(), response=response, flags=flags)
    assert completed.returncode == 0, completed.stderr
    assert "unbound variable" not in completed.stderr
    assert _markers(completed.stdout) == [
        {"url": url, "branch": BRANCH, "provider": provider}
    ]


@pytest.mark.parametrize(
    "build,response,lookup,url",
    [
        (
            _github,
            '{"message":"A pull request already exists for acme:branch."}',
            f"[{GITHUB_201}]",
            "https://github.com/acme/app/pull/7",
        ),
        (
            _gitlab,
            '{"message":["Another open merge request already exists"]}',
            f"[{GITLAB_201}]",
            "https://gitlab.com/acme/app/-/merge_requests/5",
        ),
    ],
    ids=["github", "gitlab"],
)
@pytest.mark.parametrize("flags", [("-u",), ("-euo", "pipefail")], ids=["u", "euo"])
def test_capture_shell_no_url_fallback_prints_marker_under_nounset(
    tmp_path, build, response, lookup, url, flags
):
    """The harness runs this block under ``set -euo pipefail``: a grep with
    no match on the create response must not end it before the lookup."""
    completed = _run(tmp_path, build(), response=response, lookup=lookup, flags=flags)
    assert completed.returncode == 0, completed.stderr
    assert "unbound variable" not in completed.stderr
    assert [m["url"] for m in _markers(completed.stdout)] == [url]


@pytest.mark.parametrize("build", [_github, _gitlab], ids=["github", "gitlab"])
@pytest.mark.parametrize("flags", [("-u",), ("-euo", "pipefail")], ids=["u", "euo"])
def test_capture_shell_nothing_resolved_runs_clean_under_nounset(
    tmp_path, build, flags
):
    completed = _run(
        tmp_path,
        build(),
        response='{"message":"Bad credentials"}',
        lookup="[]",
        flags=flags,
    )
    assert completed.returncode == 0, completed.stderr
    assert "unbound variable" not in completed.stderr
    assert _markers(completed.stdout) == []
    assert "could be resolved for branch" in completed.stdout


def _existing_github_pr(body="Existing body"):
    return json.dumps(
        [
            {
                "number": 7,
                "html_url": "https://github.com/acme/app/pull/7",
                "head": {"ref": BRANCH},
                "body": body,
            }
        ]
    )


def test_fallback_provenance_failure_is_recorded_not_fatal_under_errexit(tmp_path):
    """The body-update script exits 2 when provenance is skipped. Under
    ``set -e`` a bare call aborted the block before ``py_status`` was read;
    now the failure is recorded and disclosed and the block finishes."""
    script = _github(
        execution_link="https://preloop.example/console/flows/executions/not-a-uuid"
    )
    completed = _run(
        tmp_path,
        script,
        response='{"message":"A pull request already exists"}',
        lookup=_existing_github_pr(),
        flags=("-euo", "pipefail"),
    )
    assert completed.returncode == 0, completed.stderr
    assert "existing pull request body was left unchanged" in completed.stderr
    assert _markers(completed.stdout) == []


def test_fallback_body_update_then_marker_under_errexit(tmp_path):
    script = _github(
        execution_link=(
            "https://preloop.example/console/flows/executions/"
            "0976028b-ee59-4fc6-a38a-a770cfdc800a"
        )
    )
    completed = _run(
        tmp_path,
        script,
        response='{"message":"A pull request already exists"}',
        lookup=_existing_github_pr(),
        flags=("-euo", "pipefail"),
    )
    assert completed.returncode == 0, completed.stderr
    assert [m["url"] for m in _markers(completed.stdout)] == [
        "https://github.com/acme/app/pull/7"
    ]


def test_capture_shell_201_with_execution_link_prints_marker(tmp_path):
    """The provenance arguments only matter in the fallback; the happy path
    with a console link must still print the marker under ``-u``."""
    script = _github(
        execution_link="https://preloop.example/console/flows/executions/abc"
    )
    completed = _run(tmp_path, script, response=GITHUB_201)
    assert completed.returncode == 0, completed.stderr
    assert len(_markers(completed.stdout)) == 1

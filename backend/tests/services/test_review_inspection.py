"""Only completed reads of the bound change establish review inspection."""

import pytest

HEAD = "a" * 40
BASE = "b" * 40


@pytest.mark.parametrize(
    "command,expected",
    [
        (f"git diff {BASE}...{HEAD}", True),
        ('git diff "$PRELOOP_REVIEW_BASE_SHA"..."$PRELOOP_REVIEW_HEAD_SHA"', True),
        (f"/bin/bash -lc 'git diff {BASE}...{HEAD}'", True),
        ("cat src/changed.py", True),
        ("sed -n '1,120p' src/changed.py", True),
        (f"git show {HEAD}:src/changed.py", True),
        ("ls && git rev-parse HEAD", False),
        ("git log -1", False),
        (f"git diff -- {BASE}...{HEAD}", False),
        ("cat --help src/changed.py", False),
        ("head -n 0 src/changed.py", False),
        ("head -0 src/changed.py", False),
        (f"git diff {BASE}...{HEAD} -- missing.py", False),
        (f"git diff {BASE}...{HEAD} -- src/changed.py", True),
        ("cat README.md", False),
        (f"echo 'git diff {BASE}...{HEAD}'", False),
        (f"git diff --stat {BASE}...{HEAD}", False),
        (f"git diff --name-only {BASE}...{HEAD}", False),
        (f"git diff {HEAD}...{HEAD}", False),
        (f"git diff {BASE}...{HEAD} >/dev/null", False),
        (f"git diff {BASE}...{HEAD} | wc -l", False),
        (f"false; git diff {BASE}...{HEAD}", False),
    ],
)
def test_command_evidence(command: str, expected: bool) -> None:
    from preloop.services.review_inspection import command_reads_change

    assert (
        command_reads_change(command, base=BASE, head=HEAD, files=["src/changed.py"])
        is expected
    )


@pytest.mark.parametrize(
    "event,exit_code,expected",
    [
        ("item.started", None, 0),
        ("item.completed", 1, 0),
        ("item.completed", 0, 1),
    ],
)
def test_controller_requires_successful_completed_read(
    event: str, exit_code: int | None, expected: int
) -> None:
    from preloop.services.review_integrity import ObservationCounter

    counter = ObservationCounter(binding={"base_sha": BASE, "head_sha": HEAD})
    counter.add(
        {
            "observations": [
                {
                    "source": "native",
                    "kind": "command",
                    "item_id": "i",
                    "event": event,
                    "exit_code": exit_code,
                    "value": f"git diff {BASE}...{HEAD}",
                }
            ]
        }
    )
    assert counter.counts()["diff_commands"] == expected


@pytest.mark.parametrize(
    "commands",
    [
        [],
        ["pwd && ls -la", "env"],
        [
            f"git diff --stat {BASE}...{HEAD}",
            "git rev-parse HEAD",
            f"git cat-file -t {BASE}",
        ],
    ],
)
def test_observed_nonreview_sessions_do_not_satisfy_inspection(
    commands: list[str],
) -> None:
    from preloop.services.review_integrity import ObservationCounter, evaluate
    from types import SimpleNamespace

    counter = ObservationCounter(binding={"base_sha": BASE, "head_sha": HEAD})
    for index, command in enumerate(commands):
        counter.add(
            {
                "observations": [
                    {
                        "source": "native",
                        "kind": "command",
                        "item_id": str(index),
                        "event": "item.completed",
                        "exit_code": 0,
                        "value": command,
                    }
                ]
            }
        )
    outcome = evaluate(
        SimpleNamespace(
            git_clone_config={"publication_mode": "read_only"}, result_schema={}
        ),
        result={"head_sha": HEAD, "base_sha": BASE, "review": "No issues found"},
        binding={"head_sha": HEAD, "base_sha": BASE},
        diff=None,
        inspection=counter.counts(),
    )
    assert outcome["category"] == "review_not_inspected"
    assert outcome["inspection"]["diff_commands"] == 0


def test_changed_file_read_uses_admitted_checkout_path() -> None:
    from preloop.services.review_integrity import ObservationCounter, inspection_root
    from types import SimpleNamespace

    binding = {"base_sha": BASE, "head_sha": HEAD, "project_id": "project-2"}
    flow = SimpleNamespace(
        git_clone_config={
            "repositories": [
                {"project_id": "project-1", "clone_path": "wrong"},
                {"project_id": "project-2", "clone_path": "source"},
            ]
        }
    )
    root = inspection_root(flow, binding)
    assert root == "/workspace/source"
    counter = ObservationCounter(
        binding=binding, diff={"files": ["src/changed.py"]}, root=root
    )
    counter.add(
        {
            "observations": [
                {
                    "source": "native",
                    "kind": "command",
                    "event": "item.completed",
                    "exit_code": 0,
                    "item_id": "read",
                    "value": "cat /workspace/source/src/changed.py",
                }
            ]
        }
    )
    assert counter.inspected()


def test_long_review_can_establish_inspection_after_earlier_evidence_rows() -> None:
    import json
    from unittest.mock import MagicMock
    from uuid import uuid4
    from preloop.models.crud.execution_tool_evidence import inspection_summary
    from preloop.services.execution_tool_evidence import digest

    empty = {"observations": []}
    read = {
        "observations": [
            {
                "source": "native",
                "kind": "command",
                "event": "item.completed",
                "exit_code": 0,
                "item_id": "read",
                "value": f"git diff {BASE}...{HEAD}",
            }
        ]
    }

    def row(document: dict) -> tuple[dict]:
        return ({"chunks": [json.dumps(document)], "sha256": digest(document)},)

    db = MagicMock()
    query = db.query.return_value.filter.return_value.order_by.return_value
    query.offset.return_value.limit.return_value.first.side_effect = [
        row(empty)
    ] * 501 + [row(read), None]
    result = inspection_summary(
        db, execution_id=uuid4(), binding={"base_sha": BASE, "head_sha": HEAD}
    )
    assert result["diff_commands"] == 1

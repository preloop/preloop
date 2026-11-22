"""File sharding must neither import unrelated tests nor lose new tests."""

import argparse
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "run_backend_shard", ROOT / "scripts/run_backend_shard.py"
)
assert SPEC and SPEC.loader
sharding = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sharding)


def test_discovery_obeys_patterns_without_importing_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'python_files = "test_*.py"\n'
        'norecursedirs = ["excluded"]\n'
    )
    for name in (
        "test_new.py",
        "nested/test_b.py",
        "excluded/test_skip.py",
        "helper.py",
    ):
        path = tmp_path / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('must not import during selection')\n")
    assert sharding.discover_files(tmp_path) == [
        "tests/nested/test_b.py",
        "tests/test_new.py",
    ]


def test_discovery_honors_static_pytest_ignores(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'python_files = "test_*.py"\n'
        'norecursedirs = ["tests/path/skip"]\n'
    )
    for name in (
        "test_keep.py",
        "test_ignored.py",
        "test_glob_ignored.py",
        "nested/test_keep.py",
        "path/skip/test_ignored.py",
        "venv/pyvenv.cfg",
        "venv/test_ignored.py",
    ):
        path = tmp_path / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise RuntimeError('must not import during selection')\n")
    (tmp_path / "tests/conftest.py").write_text(
        'collect_ignore = ["test_ignored.py"]\n'
        'collect_ignore_glob = ["test_glob_*.py"]\n'
    )
    assert sharding.discover_files(tmp_path) == [
        "tests/nested/test_keep.py",
        "tests/test_keep.py",
    ]


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        (
            "def pytest_ignore_collect(collection_path, config):\n"
            "    return collection_path.name == 'test_example.py'\n",
            "Dynamic pytest ignore hook",
        ),
        ("collect_ignore = list(['test_example.py'])\n", "Dynamic pytest ignore"),
    ],
)
def test_discovery_rejects_dynamic_ignores(
    tmp_path: Path, declaration: str, message: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npython_files = "test_*.py"\n'
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_example.py").touch()
    (tests / "conftest.py").write_text(declaration)
    with pytest.raises(ValueError, match=message):
        sharding.discover_files(tmp_path)


def _files(tmp_path: Path, count: int) -> list[str]:
    files = []
    for n in range(count):
        path = tmp_path / f"tests/test_{n:02}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text("def test_one():\n    pass\n")
        files.append(path.relative_to(tmp_path).as_posix())
    return files


def _covered(plan: dict) -> list[str]:
    return sorted(path for shard in plan["shards"] for path in shard["files"])


@pytest.mark.parametrize("count", [1, 2, 3, 6])
def test_plan_covers_every_file_once(tmp_path: Path, count: int) -> None:
    files = _files(tmp_path, 40)
    durations = {f"{path}::test_one": float(n % 7) for n, path in enumerate(files[:30])}
    durations["tests/deleted.py::test_old"] = 10000.0
    plan = sharding.plan_shards(files, durations, count, tmp_path)
    assert len(plan["shards"]) == count
    assert all(shard["files"] for shard in plan["shards"])
    assert _covered(plan) == files
    assert plan == sharding.plan_shards(files, durations, count, tmp_path)


def test_plan_packs_longest_first_not_contiguously(tmp_path: Path) -> None:
    files = _files(tmp_path, 4)
    weights = [5.0, 5.0, 4.0, 4.0]
    durations = {f"{p}::test_one": w for p, w in zip(files, weights, strict=False)}
    plan = sharding.plan_shards(files, durations, 2, tmp_path)
    assert [shard["seconds"] for shard in plan["shards"]] == [9.0, 9.0]
    assert plan["shards"][0]["files"] == [files[0], files[2]]


def test_new_tests_weigh_the_median_test(tmp_path: Path) -> None:
    files = _files(tmp_path, 3)
    (tmp_path / files[0]).write_text(
        "def test_one():\n    pass\n\n"
        "class TestNew:\n    def test_a(self):\n        pass\n"
    )
    durations = {
        f"{files[0]}::test_one": 1.0,
        f"{files[1]}::test_one[a]": 2.0,
        f"{files[1]}::test_one[b]": 3.0,
    }
    weights, _ = sharding.file_weights(files, durations, tmp_path)
    # median node 2.0: one unseen test in files[0], an unknown file[2]
    assert weights == {files[0]: 3.0, files[1]: 5.0, files[2]: 2.0}


def test_heavy_file_is_split_at_node_level(tmp_path: Path) -> None:
    files = _files(tmp_path, 7)
    durations = {f"{p}::test_one": 1.0 for p in files[1:]}
    heavy = files[0]
    durations.update({f"{heavy}::test_n[{i}]": 1.0 for i in range(12)})
    plan = sharding.plan_shards(files, durations, 3, tmp_path)
    assert plan["split_files"] == [heavy]
    owners = [shard for shard in plan["shards"] if heavy in shard["files"]]
    assert len(owners) > 1
    heavy_nodes = {n for n in durations if n.startswith(heavy)}
    run = [heavy_nodes - set(shard["deselect"]) for shard in owners]
    # Every recorded node runs in exactly one owning shard.
    assert sorted(n for nodes in run for n in nodes) == sorted(heavy_nodes)
    peak, mean = sharding.spread(plan)
    assert peak <= 1.15 * mean


def test_split_never_deselects_an_owned_sibling_by_prefix(tmp_path: Path) -> None:
    """pytest deselects by prefix; ``test_get`` must not hide ``test_get_user``."""
    files = _files(tmp_path, 7)
    durations = {f"{p}::test_one": 1.0 for p in files[1:]}
    heavy = files[0]
    names = ["test_get", "test_get_user", "test_get_user_admin", "test_put"]
    names += [f"test_n{i}" for i in range(8)]
    durations.update({f"{heavy}::{name}": 1.0 for name in names})
    plan = sharding.plan_shards(files, durations, 3, tmp_path)
    assert plan["split_files"] == [heavy]
    selected: set[str] = set()
    for shard in plan["shards"]:
        if heavy not in shard["files"]:
            continue
        prefixes = tuple(shard["deselect"])
        selected |= {
            node
            for node in durations
            if node.startswith(heavy) and not node.startswith(prefixes)
        }
    assert selected == {n for n in durations if n.startswith(heavy)}


def test_plan_command_fails_on_predicted_imbalance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = _files(tmp_path, 2)
    durations = tmp_path / "durations.json"
    durations.write_text(
        json.dumps({f"{files[0]}::test_one": 9.0, f"{files[1]}::test_one": 1.0})
    )
    monkeypatch.setattr(sharding, "discover_files", lambda root: files)
    args = argparse.Namespace(
        shards=2, durations=durations, out=tmp_path / "plan.json", max_ratio=1.15
    )
    assert sharding.command_plan(args, tmp_path) == 1
    args.max_ratio = 2.0
    assert sharding.command_plan(args, tmp_path) == 0
    assert json.loads(args.out.read_text())["predicted_max_over_mean"] == 1.8


def test_run_uses_worksteal_and_worker_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {"shards": [{"files": ["a.py"], "deselect": ["a.py::t"], "seconds": 1}]}
        )
    )
    calls: list[tuple[list[str], dict[str, str]]] = []
    cloned: list[int] = []
    monkeypatch.setattr(
        sharding.subprocess,
        "run",
        lambda cmd, cwd, env: (
            calls.append((cmd, env)) or argparse.Namespace(returncode=0)
        ),
    )
    fake = types.ModuleType("ci_postgres")
    fake.clone_worker_databases = cloned.append  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ci_postgres", fake)
    args = argparse.Namespace(plan=plan, group=1, workers="3", pytest_args=["--", "-q"])
    assert sharding.command_run(args, tmp_path) == 0
    command, env = calls[0]
    assert cloned == [3]
    assert env["PRELOOP_CI_WORKER_DATABASES"] == "1"
    assert command[3:] == [
        "a.py",
        "--deselect",
        "a.py::t",
        "-n",
        "3",
        "--dist",
        "worksteal",
        "-q",
    ]


@pytest.mark.parametrize("count", [0, 3])
def test_rejects_empty_shards(tmp_path: Path, count: int) -> None:
    with pytest.raises(ValueError):
        sharding.plan_shards(["test_a.py", "test_b.py"], {}, count, tmp_path)


def test_real_plan_covers_new_files_and_deleted_timings() -> None:
    files = sharding.discover_files(ROOT)
    plan = sharding.plan_shards(files, {}, 6, ROOT)
    assert "backend/tests/test_backend_shard_selection.py" in files
    assert _covered(plan) == files


def test_committed_durations_plan_within_the_balance_gate() -> None:
    durations = json.loads((ROOT / ".github/backend-shard-durations.json").read_text())
    plan = sharding.plan_shards(sharding.discover_files(ROOT), durations, 6, ROOT)
    peak, mean = sharding.spread(plan)
    assert peak <= sharding.DEFAULT_MAX_RATIO * mean


def test_plan_command_packs_only_selected_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selection receipt narrows the plan; a removed file is ignored."""
    files = _files(tmp_path, 4)
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({f"{f}::test_one": 1.0 for f in files}))
    receipt = tmp_path / "ci-selection.json"
    receipt.write_text(
        json.dumps({"candidate_tests": [files[2], files[0], "backend/tests/gone.py"]})
    )
    monkeypatch.setattr(sharding, "discover_files", lambda root: files)
    args = argparse.Namespace(
        shards=2,
        durations=durations,
        out=tmp_path / "plan.json",
        max_ratio=1.15,
        selection=receipt,
    )
    assert sharding.command_plan(args, tmp_path) == 0
    plan = json.loads(args.out.read_text())
    planned = sorted(f for shard in plan["shards"] for f in shard["files"])
    assert planned == sorted([files[0], files[2]])


def test_plan_command_rejects_an_empty_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty selection fails loudly, never falls back to all of pytest."""
    files = _files(tmp_path, 2)
    durations = tmp_path / "durations.json"
    durations.write_text("{}")
    receipt = tmp_path / "ci-selection.json"
    receipt.write_text(json.dumps({"candidate_tests": []}))
    monkeypatch.setattr(sharding, "discover_files", lambda root: files)
    args = argparse.Namespace(
        shards=1,
        durations=durations,
        out=tmp_path / "plan.json",
        max_ratio=1.15,
        selection=receipt,
    )
    assert sharding.command_plan(args, tmp_path) == 2
    assert not args.out.exists()

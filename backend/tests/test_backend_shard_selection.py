"""File sharding must neither import unrelated tests nor lose new tests."""

import importlib.util
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


@pytest.mark.parametrize("count", [1, 2, 3, 18])
def test_partition_covers_every_file_once_in_order(count: int) -> None:
    files = [f"tests/test_{n:02}.py" for n in range(40)]
    durations = {f"{path}::test_one": float(n % 7) for n, path in enumerate(files[:30])}
    durations["tests/deleted.py::test_old"] = 10000.0
    groups = sharding.partition_files(files, durations, count)
    assert len(groups) == count
    assert all(groups)
    assert [path for group in groups for path in group] == files
    assert groups == sharding.partition_files(files, durations, count)


def test_partition_uses_aggregate_file_duration() -> None:
    files = [f"test_{i}.py" for i in range(6)]
    durations = {f"{path}::test_a": 1.0 for path in files}
    durations["test_0.py::test_b"] = 4.0
    assert sharding.partition_files(files, durations, 2) == [files[:1], files[1:]]


@pytest.mark.parametrize("count", [0, 3])
def test_rejects_empty_shards(count: int) -> None:
    with pytest.raises(ValueError):
        sharding.partition_files(["test_a.py", "test_b.py"], {}, count)


def test_real_plan_covers_new_files_and_deleted_timings() -> None:
    files = sharding.discover_files(ROOT)
    groups = sharding.partition_files(files, {}, 18)
    assert "backend/tests/test_backend_shard_selection.py" in files
    assert [path for group in groups for path in group] == files

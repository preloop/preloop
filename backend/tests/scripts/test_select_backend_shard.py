"""Backend CI shards stay balanced and keep each test file on one runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "select_backend_shard", REPO_ROOT / "scripts" / "select_backend_shard.py"
)
assert SPEC is not None and SPEC.loader is not None
select_backend_shard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(select_backend_shard)


def test_recorded_files_land_in_one_shard_of_similar_length() -> None:
    records = select_backend_shard.load_records(
        REPO_ROOT / "scripts" / "backend_test_durations.json"
    )
    groups = select_backend_shard.shard_paths(records, 8)
    seen: set[str] = set()
    durations: list[float] = []
    lookup = dict(records)
    for group in groups:
        assert group
        for path in group:
            assert path not in seen
            seen.add(path)
        durations.append(sum(lookup[path] for path in group))
    assert seen == set(lookup)
    # Equal-count shards put ~1400s in group 2. Duration slices stay near
    # the 341s mean; the slowest recorded file is a single 120s test.
    assert max(durations) < 450


def test_a_new_file_is_still_scheduled() -> None:
    records = [("backend/tests/test_old.py", 10.0)]
    groups = select_backend_shard.shard_paths(
        records, 2, extra=[("backend/tests/test_new.py", 10.0)]
    )
    scheduled = [path for group in groups for path in group]
    assert scheduled.count("backend/tests/test_new.py") == 1
    assert set(scheduled) == {
        "backend/tests/test_old.py",
        "backend/tests/test_new.py",
    }


def test_group_numbers_are_one_based(tmp_path: Path) -> None:
    durations = tmp_path / "durations.json"
    durations.write_text(
        '[{"path": "backend/tests/test_old.py", "seconds": 1}]\n',
        encoding="utf-8",
    )
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_old.py").write_text(
        "def test_old():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "backend" / "tests" / "test_new.py").write_text(
        "def test_new():\n    pass\n", encoding="utf-8"
    )
    first = select_backend_shard.paths_for_group(tmp_path, durations, 1, 1)
    assert "backend/tests/test_old.py" in first
    assert "backend/tests/test_new.py" in first

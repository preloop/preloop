"""Import-graph backend test selection, shard sizing and main-push diffs."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_import_graph as graph_mod  # noqa: E402
import ci_select  # noqa: E402
from ci_select import SMOKE, impacted_tests, select, shard_count  # noqa: E402

POLICY = json.loads((ROOT / ".github/ci-policy.json").read_text())


def refs(source: str, module: str = "preloop.pkg.mod", **kwargs: bool) -> dict:
    """Parse one synthetic module."""
    return graph_mod.direct_references(source, module, False, **kwargs)


def test_module_level_imports_are_top_and_function_imports_lazy() -> None:
    """Deferred imports are recorded apart from import-time ones."""
    result = refs(
        "import preloop.a\n"
        "from preloop.b import c\n"
        "try:\n    import preloop.d\nexcept ImportError:\n    pass\n"
        "class K:\n    from preloop import e\n"
        "def f():\n    import preloop.lazy\n"
        "async def g():\n    from preloop.other import thing\n"
        "import os\n"
    )
    assert result["top"] == [
        "preloop",
        "preloop.a",
        "preloop.b",
        "preloop.b.c",
        "preloop.d",
        "preloop.e",
    ]
    assert result["lazy"] == ["preloop.lazy", "preloop.other", "preloop.other.thing"]


def test_type_checking_imports_and_dotted_strings_are_lazy_in_source() -> None:
    """Annotation-only imports and import_module strings do not run at import."""
    result = refs(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from preloop.typed import T\n"
        "else:\n    import preloop.runtime\n"
        "NAME = 'preloop.plugins.loader'\n"
    )
    assert result["top"] == ["preloop.runtime"]
    assert result["lazy"] == [
        "preloop.plugins.loader",
        "preloop.typed",
        "preloop.typed.T",
    ]


def test_test_files_count_every_reference_as_top() -> None:
    """A test runs the code that holds its imports and its patch targets."""
    result = refs(
        "def test_x():\n"
        "    from preloop.services import widget\n"
        "    with patch('preloop.services.gadget.build'):\n"
        "        pass\n",
        "tests.services.test_x",
        is_test=True,
    )
    assert result["lazy"] == []
    assert "preloop.services.widget" in result["top"]
    assert "preloop.services.gadget.build" in result["top"]


def test_relative_imports_resolve_against_the_package() -> None:
    """``from . import x`` and ``from ..y import z`` name absolute modules."""
    module = graph_mod.direct_references(
        "from . import sibling\nfrom ..up import thing\n", "preloop.pkg.mod", False
    )
    package = graph_mod.direct_references("from .child import x\n", "preloop.pkg", True)
    assert "preloop.pkg.sibling" in module["top"]
    assert "preloop.up.thing" in module["top"]
    assert "preloop.pkg.child" in package["top"]


def test_unparseable_source_has_no_references() -> None:
    """Pytest reports the syntax error; the graph must not crash on it."""
    assert refs("def broken(:\n") == {"top": [], "lazy": []}


GRAPH = {
    "backend/preloop/__init__.py": {"top": [], "lazy": []},
    "backend/preloop/core.py": {"top": [], "lazy": []},
    "backend/preloop/service.py": {"top": ["preloop.core"], "lazy": []},
    "backend/preloop/api.py": {"top": ["preloop.service"], "lazy": ["preloop.late"]},
    "backend/preloop/late.py": {"top": ["preloop.core"], "lazy": ["preloop.later"]},
    "backend/preloop/later.py": {"top": [], "lazy": []},
    "backend/tests/test_api.py": {"top": ["preloop.api"], "lazy": []},
    "backend/tests/test_core.py": {"top": ["preloop.core.helper"], "lazy": []},
    "backend/tests/test_gone.py": {"top": ["preloop.removed"], "lazy": []},
}


def test_top_imports_are_transitive_and_include_packages() -> None:
    """A test reaches modules through any chain and every parent package."""
    found = graph_mod.importers(GRAPH, ["preloop.core"], lazy_hops=0)
    assert found == {
        "backend/preloop/core.py",
        "backend/preloop/service.py",
        "backend/preloop/api.py",
        "backend/preloop/late.py",
        "backend/tests/test_api.py",
        "backend/tests/test_core.py",
    }
    package = graph_mod.importers(GRAPH, ["preloop"], lazy_hops=0)
    assert "backend/tests/test_gone.py" in package


def test_lazy_imports_are_crossed_a_bounded_number_of_times() -> None:
    """One hop reaches ``late`` from ``api``; ``later`` needs a second hop."""
    one = graph_mod.importers(GRAPH, ["preloop.late"], lazy_hops=1)
    assert "backend/tests/test_api.py" in one
    assert "backend/tests/test_api.py" not in graph_mod.importers(
        GRAPH, ["preloop.late"], lazy_hops=0
    )
    assert "backend/tests/test_api.py" not in graph_mod.importers(
        GRAPH, ["preloop.later"], lazy_hops=1
    )
    assert "backend/tests/test_api.py" in graph_mod.importers(
        GRAPH, ["preloop.later"], lazy_hops=2
    )


def test_deleted_module_still_selects_its_old_importers() -> None:
    """A test that imports a removed module must run to show the break."""
    found = graph_mod.importers(GRAPH, ["preloop.removed"])
    assert found == {"backend/tests/test_gone.py"}


def test_build_reuses_cached_parses_by_blob(tmp_path: Path) -> None:
    """Only files whose blob changed are reparsed; the cache is rewritten."""

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    src = tmp_path / "backend/preloop"
    src.mkdir(parents=True)
    (src / "a.py").write_text("import preloop.b\n")
    (src / "b.py").write_text("")
    git("add", ".")
    cache = tmp_path / "cache.json"
    first = graph_mod.build(tmp_path, cache)
    assert first["backend/preloop/a.py"]["top"] == ["preloop.b"]
    data = json.loads(cache.read_text())
    data["files"]["backend/preloop/b.py"]["refs"] = {
        "top": ["preloop.cached"],
        "lazy": [],
    }
    cache.write_text(json.dumps(data))
    (src / "a.py").write_text("import preloop.c\n")
    second = graph_mod.build(tmp_path, cache)
    # b.py is unchanged, so its (doctored) cached parse is reused; the
    # modified a.py is reparsed from the working tree.
    assert second["backend/preloop/b.py"]["top"] == ["preloop.cached"]
    assert second["backend/preloop/a.py"]["top"] == ["preloop.c"]


INVENTORY = ["backend/tests/test_api.py", "backend/tests/test_core.py", *SMOKE]
DURATIONS = {f"{test}::t": 1.0 for test in SMOKE} | {
    "backend/tests/test_api.py::t": 600.0,
    "backend/tests/test_core.py::t": 600.0,
}
SOURCES = {
    "backend/preloop/late.py": "TEMPLATE = 'templates/review_note.md.j2'\n",
}


def read(path: str) -> str:
    """Synthetic sources keyed by path."""
    return SOURCES.get(path, "")


def test_data_file_selects_tests_of_the_module_that_names_it() -> None:
    """A template reaches tests through the module that loads it by name."""
    tests, reason = impacted_tests(
        ["backend/preloop/templates/review_note.md.j2"], INVENTORY, read, GRAPH
    )
    assert tests == ["backend/tests/test_api.py"]
    assert reason.startswith("import graph: 1 ")


def test_generic_file_name_alone_selects_nothing() -> None:
    """``Dockerfile`` is too common a name to tie a file to one module."""
    sources = {"backend/preloop/core.py": "build('Dockerfile')\n"}
    tests, _ = impacted_tests(
        ["environments/scanner/Dockerfile"],
        INVENTORY,
        lambda path: sources.get(path, ""),
        GRAPH,
    )
    assert tests == []


def test_changed_test_file_selects_itself() -> None:
    """Changed test files always run, even with no importers."""
    tests, _ = impacted_tests(["backend/tests/test_core.py"], INVENTORY, read, GRAPH)
    assert tests == ["backend/tests/test_core.py"]


def test_main_push_selects_by_diff_and_keeps_every_job() -> None:
    """A main push narrows backend tests but still runs every other job."""
    result = select(
        ["backend/preloop/core.py"],
        POLICY,
        INVENTORY,
        event="push",
        ref="refs/heads/main",
        read=read,
        graph=GRAPH,
    )
    assert result["full"] and all(result["live"].values())
    assert not result["backend_full"]
    assert result["candidate_tests"] == sorted(INVENTORY)
    assert result["backend_count"] == len(INVENTORY)
    assert result["impact_reason"].startswith("import graph:")


def test_main_push_outside_backend_runs_only_smoke() -> None:
    """A docs-only merge to main no longer pays for the whole suite."""
    result = select(
        ["docs/index.md"],
        POLICY,
        INVENTORY,
        event="push",
        ref="refs/heads/main",
        durations=DURATIONS,
        read=read,
        graph=GRAPH,
    )
    assert result["candidate_tests"] == sorted(SMOKE)
    assert result["backend_shards"] == 1


@pytest.mark.parametrize(
    ("paths", "attempt", "reason"),
    [
        (["backend/preloop/core.py"], 2, "full main rerun"),
        ([], 1, "empty or unavailable main diff"),
    ],
)
def test_main_rerun_or_missing_diff_runs_everything(
    paths: list[str], attempt: int, reason: str
) -> None:
    """A full rerun of main, or a diff we could not compute, is full."""
    result = select(
        paths,
        POLICY,
        INVENTORY,
        event="push",
        ref="refs/heads/main",
        attempt=attempt,
        read=read,
        graph=GRAPH,
    )
    assert result["backend_full"] and result["impact_reason"] == reason
    assert result["backend_shards"] == 6


def test_security_path_runs_every_job_but_selects_tests_by_graph() -> None:
    """Sensitive paths widen jobs; their tests come from the import graph."""
    path = "backend/preloop/services/rate_limit.py"
    result = select([path], POLICY, INVENTORY, read=read, graph=GRAPH)
    assert result["full"] and all(result["live"].values())
    assert not result["backend_full"]
    assert result["candidate_tests"] == sorted(SMOKE)


def test_ci_full_label_selects_every_test() -> None:
    """``ci:full`` keeps meaning the whole suite."""
    result = select(
        ["backend/preloop/core.py"],
        POLICY,
        INVENTORY,
        labels=("ci:full",),
        read=read,
        graph=GRAPH,
    )
    assert result["backend_full"] and result["impact_reason"] == "ci:full"


def test_shard_count_scales_with_estimated_time() -> None:
    """Narrow selections use fewer shards of about a full shard's length."""
    inventory = [f"backend/tests/test_{i}.py" for i in range(36)]
    durations = {f"{test}::t": 10.0 for test in inventory}
    assert shard_count(inventory, inventory, durations) == 6
    assert shard_count(inventory[:1], inventory, durations) == 1
    assert shard_count(inventory[:7], inventory, durations) == 2
    assert shard_count(inventory[:35], inventory, durations) == 6
    assert shard_count([], inventory, durations) == 1


def test_last_green_main_without_token_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token (forks, local runs) means the caller uses ``before``."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert ci_select.last_green_main("HEAD") == ""


def test_last_green_main_skips_non_ancestors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A green run from a rewritten or unrelated history is never a base."""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    parent = subprocess.check_output(
        ["git", "rev-parse", "HEAD^"], text=True, cwd=ROOT
    ).strip()
    runs = {
        "workflow_runs": [
            {"head_sha": head},
            {"head_sha": "f" * 40},
            {"head_sha": parent},
        ]
    }

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(runs).encode()

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setattr(ci_select.urllib.request, "urlopen", lambda *a, **k: Response())
    monkeypatch.chdir(ROOT)
    assert ci_select.last_green_main(head) == parent


def test_real_repository_graph_selects_a_bounded_subset() -> None:
    """On this checkout a leaf service change selects well under the suite."""
    inventory = ci_select.discover_files(ROOT)
    tests, reason = impacted_tests(
        ["backend/preloop/services/db_pool_monitor.py"], inventory
    )
    assert reason.startswith("import graph:")
    assert 0 < len(tests) < len(inventory) // 2


CONFTEST = """
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def app(db_session):
    return create_app()

@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c

@pytest.fixture(name="authed")
def _authed(client):
    return client

@pytest.fixture
def db_session():
    return None
"""


def test_app_fixture_tests_follow_fixture_dependencies() -> None:
    """Tests requesting app, client or a fixture built on them are found."""
    sources = {
        "backend/tests/conftest.py": CONFTEST,
        "backend/tests/test_http.py": "def test_get(client):\n    pass\n",
        "backend/tests/test_named.py": "async def test_x(authed):\n    pass\n",
        "backend/tests/test_marked.py": (
            "import pytest\n"
            "pytestmark = pytest.mark.usefixtures('app')\n"
            "def test_x():\n    pass\n"
        ),
        "backend/tests/test_plain.py": "def test_x(db_session):\n    pass\n",
        "backend/tests/test_broken.py": "def broken(:\n",
    }
    assert graph_mod.app_fixture_tests(sources) == {
        "backend/tests/test_http.py",
        "backend/tests/test_named.py",
        "backend/tests/test_marked.py",
    }


def test_api_change_selects_tests_that_reach_it_over_http() -> None:
    """A router change runs client-fixture tests that never import it."""
    graph = GRAPH | {
        "backend/preloop/api/routes.py": {"top": [], "lazy": []},
        "backend/tests/conftest.py": {"top": [], "lazy": []},
        "backend/tests/test_http.py": {"top": [], "lazy": []},
    }
    sources = {
        "backend/tests/conftest.py": CONFTEST,
        "backend/tests/test_http.py": "def test_get(client):\n    pass\n",
    }
    inventory = [*INVENTORY, "backend/tests/test_http.py"]
    tests, _ = impacted_tests(
        ["backend/preloop/api/routes.py"],
        inventory,
        lambda path: sources.get(path, ""),
        graph,
    )
    assert tests == ["backend/tests/test_http.py"]
    service, _ = impacted_tests(
        ["backend/preloop/later.py"],
        inventory,
        lambda path: sources.get(path, ""),
        graph,
    )
    assert "backend/tests/test_http.py" not in service

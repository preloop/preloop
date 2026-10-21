"""Catalog listing metadata: MCP registry server.json, citation and Docker Hub files."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str) -> ModuleType:
    """Import a script from scripts/ by file path.

    Args:
        name: Script module name without ``.py``.

    Returns:
        The loaded module.
    """
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load("validate_server_json")
release = _load("release")
VERSION = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _server() -> dict[str, Any]:
    """Return the committed server.json.

    Returns:
        The parsed document.
    """
    return json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))


def test_committed_server_json_is_valid() -> None:
    """server.json passes the vendored registry schema and matches VERSION."""
    server = _server()

    assert validator.validate(server, VERSION) == []
    assert server["name"] == "io.github.preloop/preloop"
    assert server["remotes"][0]["url"] == "https://preloop.ai/mcp/v1"
    assert validator.main([]) == 0


def test_description_over_registry_limit_is_rejected() -> None:
    """The registry caps descriptions at 100 characters."""
    server = _server()
    server["description"] = "x" * 101

    problems = validator.validate(server, VERSION)

    assert any(p.startswith("description:") for p in problems)


def test_version_drift_and_unknown_schema_are_rejected() -> None:
    """A stale version or an unvendored schema date fails validation."""
    server = _server()
    assert validator.validate(server, "0.0.1") != []

    other = copy.deepcopy(server)
    other["$schema"] = (
        "https://static.modelcontextprotocol.io/schemas/1999-01-01/server.schema.json"
    )
    assert "no vendored schema" in validator.validate(other, VERSION)[0]

    other["$schema"] = "https://example.com/schema.json"
    assert validator.validate(other, VERSION) != []


def test_release_bumps_server_json_and_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scripts/release.py moves every listing version with the release."""
    for name in ("server.json", "CITATION.cff", "codemeta.json"):
        shutil.copy(REPO_ROOT / name, tmp_path / name)
    monkeypatch.setattr(release, "ROOT_DIR", tmp_path)
    plan = release.ReleasePlan(version="9.8.7", release_date="2030-01-01")

    release.update_server_json(plan)
    release.update_citation_metadata(plan)

    assert json.loads((tmp_path / "server.json").read_text())["version"] == "9.8.7"
    assert json.loads((tmp_path / "codemeta.json").read_text())["version"] == "9.8.7"
    citation = yaml.safe_load((tmp_path / "CITATION.cff").read_text())
    assert citation["version"] == "9.8.7"


def test_citation_and_codemeta_track_the_release() -> None:
    """CITATION.cff and codemeta.json parse and carry the current version."""
    citation = yaml.safe_load((REPO_ROOT / "CITATION.cff").read_text())
    codemeta = json.loads((REPO_ROOT / "codemeta.json").read_text())

    assert citation["cff-version"] == "1.2.0"
    assert citation["license"] == "Apache-2.0"
    assert citation["version"] == VERSION
    assert codemeta["version"] == VERSION


@pytest.mark.parametrize("image", ["preloop", "console"])
def test_dockerhub_descriptions_fit(image: str) -> None:
    """Docker Hub short descriptions stay within its 100-character limit."""
    directory = REPO_ROOT / "docker" / "dockerhub"
    short = (directory / f"{image}.short.txt").read_text().splitlines()[0]

    assert 0 < len(short) <= 100
    assert (directory / f"{image}.md").read_text().strip()


def test_artifacthub_metadata() -> None:
    """Artifact Hub repository file and chart annotations are well formed."""
    repo = yaml.safe_load((REPO_ROOT / "helm" / "artifacthub-repo.yml").read_text())
    chart = yaml.safe_load((REPO_ROOT / "helm" / "preloop" / "Chart.yaml").read_text())

    assert repo["owners"][0]["email"]
    annotations = chart["annotations"]
    assert annotations["artifacthub.io/license"] == "Apache-2.0"
    links = yaml.safe_load(annotations["artifacthub.io/links"])
    assert {link["name"] for link in links} >= {"Documentation", "Source"}

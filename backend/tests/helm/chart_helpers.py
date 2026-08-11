"""Shared helpers for the helm chart guard tests.

Both suites in this directory need the same primitives: the chart's default
values, dotted ``.Values`` lookup, and a way to run a real ``helm template``
without network access. They live here so the suites cannot drift apart.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "helm" / "preloop"
CHART_VALUES = CHART_DIR / "values.yaml"


def load_values() -> Dict:
    """Return the chart's default values."""
    return yaml.safe_load(CHART_VALUES.read_text())


def resolve_values_path(values: Dict, dotted: str):
    """Look up a dotted ``.Values`` path, returning None when absent."""
    node = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@contextmanager
def offline_chart() -> Iterator[Path]:
    """Yield a copy of the chart that renders without fetching dependencies.

    ``helm template`` refuses to run while a declared subchart is missing from
    ``charts/``, which would make these tests require network access. The
    subcharts are irrelevant to the templates under test, so the copy simply
    declares none.
    """
    with tempfile.TemporaryDirectory() as tmp:
        chart_copy = Path(tmp) / CHART_DIR.name
        shutil.copytree(CHART_DIR, chart_copy)
        chart_yaml = chart_copy / "Chart.yaml"
        metadata = yaml.safe_load(chart_yaml.read_text())
        metadata.pop("dependencies", None)
        chart_yaml.write_text(yaml.safe_dump(metadata))
        yield chart_copy


def helm_template(template: str, overrides: List[str] | None = None) -> str:
    """Render one chart template, skipping when helm is unavailable."""
    helm = shutil.which("helm")
    if helm is None:  # pragma: no cover - depends on the local toolchain
        pytest.skip("helm binary not available")

    with offline_chart() as chart_dir:
        command = [helm, "template", "preloop", str(chart_dir), "--show-only", template]
        for override in overrides or []:
            command += ["--set", override]
        result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    return result.stdout

"""Shared loaders for CRA result fixtures."""

from __future__ import annotations

import copy
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_json(*parts: str) -> dict[str, Any]:
    """Load a JSON fixture relative to backend/tests/fixtures."""
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


def make_evidence_archive(result: dict[str, Any]) -> bytes:
    """Build a gzip tar with result.json and an evidence/ member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        packed = json.dumps(result).encode("utf-8")
        info = tarfile.TarInfo("result.json")
        info.size = len(packed)
        tar.addfile(info, io.BytesIO(packed))
        note = b"# CRA evidence member\n"
        info = tarfile.TarInfo("evidence/sbom-verify.md")
        info.size = len(note)
        tar.addfile(info, io.BytesIO(note))
    return buf.getvalue()


@pytest.fixture
def sbomaudit_result() -> dict[str, Any]:
    return load_json("cra", "result-sbomaudit.json")


@pytest.fixture
def vulnscan_result() -> dict[str, Any]:
    return load_json("cra", "result-vulnscan.json")


@pytest.fixture
def releaseaudit_result() -> dict[str, Any]:
    return load_json("cra", "result-releaseaudit.json")


@pytest.fixture
def duediligence_result() -> dict[str, Any]:
    return load_json("evidence", "due-diligence-record.json")


def clone(payload: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(payload)

"""docs/guide/artifacts.md must match the artifact code it documents (#1090).

The guide lists kinds, media types, caps, reserved label keys and error codes.
These tests read the same constants the deposit path uses, so a new kind, a
changed cap or a new error code fails here until the guide says so.
"""

from __future__ import annotations

import re
from pathlib import Path

from preloop.config import Settings
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.services import artifact_deposit, artifact_mcp_tools, artifact_shapes
from preloop.services.artifact_media import _ALLOWED, ARTIFACT_KINDS

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDE = (REPO_ROOT / "docs" / "guide" / "artifacts.md").read_text()


def _kind_rows() -> dict[str, str]:
    rows = {}
    for line in GUIDE.splitlines():
        match = re.match(r"\| `([a-z_]+)` \|(.*)\|$", line)
        if match and match.group(1) in ARTIFACT_KINDS:
            rows[match.group(1)] = match.group(2)
    return rows


def _mib(value: int) -> str:
    return f"{value // 1024**2} MiB"


def test_every_kind_has_a_row_with_its_media_types_cap_and_setting():
    rows = _kind_rows()
    assert set(rows) == set(ARTIFACT_KINDS)
    fields = Settings.model_fields
    for kind in ARTIFACT_KINDS:
        row = rows[kind]
        for media_type in _ALLOWED.get(kind, {}):
            assert f"`{media_type}`" in row, (kind, media_type)
        setting = f"runtime_session_{kind}_max_bytes"
        assert f"`{setting.upper()}`" in row, kind
        assert _mib(fields[setting].default) in row, kind


def test_reserved_label_keys_are_documented():
    for key in crud_artifact.RESERVED_LABEL_KEYS:
        assert f"| `{key}` |" in GUIDE, key


def test_every_error_code_is_documented_with_its_status():
    codes = {
        value
        for module in (artifact_deposit, artifact_mcp_tools, artifact_shapes)
        for name, value in vars(module).items()
        if name.startswith("ERROR_") and isinstance(value, str)
    }
    assert codes, "no error constants found"
    # The deposit service answers this one as artifact_too_large.
    codes.discard(artifact_shapes.ERROR_BLOCK_TOO_LARGE)
    for code in codes:
        assert f"`{code}`" in GUIDE, code
    for code, status in artifact_deposit._STATUS_BY_CODE.items():
        if code == artifact_shapes.ERROR_BLOCK_TOO_LARGE:
            continue  # answered as artifact_too_large
        assert re.search(rf"\| {status} \| `{code}` \|", GUIDE), (code, status)


def test_list_limits_match():
    assert f"`limit` is 1 to {artifact_deposit.LIST_LIMIT_MAX}" in GUIDE
    assert f"(default {artifact_deposit.LIST_LIMIT_DEFAULT})" in GUIDE

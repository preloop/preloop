"""Tests for the harness test doubles.

Outside the project pytest testpaths on purpose; verify.sh runs it as step 0
(`python3 stub/test_harness_server.py`). It also runs under pytest:
python -m pytest -c /dev/null stub/test_harness_server.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness_server  # noqa: E402


def test_safe_header_keeps_ordinary_headers() -> None:
    assert harness_server._safe_header("retry-after", "5") == ("retry-after", "5")


def test_safe_header_drops_crlf_values() -> None:
    assert harness_server._safe_header("x-a", "1\r\nSet-Cookie: y=1") is None
    assert harness_server._safe_header("x-a", "1\nx") is None


def test_safe_header_drops_non_token_names() -> None:
    assert harness_server._safe_header("bad name", "v") is None
    assert harness_server._safe_header("x-a\r\n", "v") is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(tests)} harness_server tests passed")

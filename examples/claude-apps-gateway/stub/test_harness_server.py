"""Tests for the harness test doubles.

Run from stub/: python -m pytest -c /dev/null test_harness_server.py
"""

from __future__ import annotations

import harness_server


def test_safe_header_keeps_ordinary_headers() -> None:
    assert harness_server._safe_header("retry-after", "5") == ("retry-after", "5")


def test_safe_header_drops_crlf_values() -> None:
    assert harness_server._safe_header("x-a", "1\r\nSet-Cookie: y=1") is None
    assert harness_server._safe_header("x-a", "1\nx") is None


def test_safe_header_drops_non_token_names() -> None:
    assert harness_server._safe_header("bad name", "v") is None
    assert harness_server._safe_header("x-a\r\n", "v") is None

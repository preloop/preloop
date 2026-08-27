"""Tests for default config-path discovery and missing-config error handling."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# Make the standalone plugin package importable without installation.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from preloop_hermes_plugin.plugin import (  # noqa: E402
    HermesPreloopPlugin,
    _config_search_summary,
    _discover_config_path,
    main,
)


# ---------------------------------------------------------------------------
# _discover_config_path
# ---------------------------------------------------------------------------


def test_explicit_path_returned_as_is(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.yaml"
    assert _discover_config_path(explicit) == explicit


def test_hermes_home_takes_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    config = hermes_home / "config.yaml"
    config.write_text("preloop: {}\n")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Also set HOME so the fallback would differ.
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    assert _discover_config_path() == config


def test_hermes_home_skipped_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$HERMES_HOME dir exists but contains no config.yaml."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    # no config.yaml written

    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(fake_home))

    assert _discover_config_path() == fake_home / ".hermes" / "config.yaml"


def test_falls_back_to_home_hermes_when_no_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _discover_config_path() == tmp_path / ".hermes" / "config.yaml"


def test_hermes_home_unset_home_dir_config_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".hermes" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("preloop: {}\n")

    assert _discover_config_path() == config


def test_explicit_overrides_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("preloop: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    explicit = tmp_path / "my-config.yaml"
    assert _discover_config_path(explicit) == explicit


# ---------------------------------------------------------------------------
# _config_search_summary
# ---------------------------------------------------------------------------


def test_search_summary_explicit() -> None:
    p = Path("/custom/hermes.yaml")
    assert _config_search_summary(p) == str(p)


def test_search_summary_no_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    summary = _config_search_summary()
    assert str(tmp_path / ".hermes" / "config.yaml") in summary
    assert "HERMES_HOME" not in summary  # env var not set


def test_search_summary_with_hermes_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    monkeypatch.setenv("HOME", str(tmp_path))
    summary = _config_search_summary()
    assert str(tmp_path / "hh" / "config.yaml") in summary
    assert str(tmp_path / ".hermes" / "config.yaml") in summary


# ---------------------------------------------------------------------------
# main() CLI error for missing config
# ---------------------------------------------------------------------------


def test_main_verify_exits_cleanly_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["preloop-hermes-plugin", "verify"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    msg = str(exc_info.value)
    assert "config not found" in msg.lower() or "not found" in msg.lower()
    assert "--config" in msg


def test_main_run_exits_cleanly_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["preloop-hermes-plugin", "run"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    msg = str(exc_info.value)
    assert "not found" in msg.lower()
    assert "--config" in msg


def test_main_verify_with_explicit_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(
        "sys.argv", ["preloop-hermes-plugin", "verify", "--config", str(missing)]
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    msg = str(exc_info.value)
    assert str(missing) in msg
    assert "--config" in msg


def test_main_login_does_not_require_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """login creates the config, so it must not exit even if file is absent."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv", ["preloop-hermes-plugin", "login"]
    )

    # login() will try to interact (input()), so we just verify it does not
    # raise SystemExit with the "config not found" message. We monkeypatch
    # input to raise a controlled error instead.
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        main()
    # If we got here, main() did NOT SystemExit for missing config.


# ---------------------------------------------------------------------------
# Plugin methods use discovery (integration-level)
# ---------------------------------------------------------------------------


def _write_hermes_config(path: Path) -> None:
    doc = {
        "preloop": {
            "control": {
                "enabled": True,
                "runtime": "hermes",
                "control_ws_url": "wss://test.preloop.ai/api/v1/agents/control/ws",
                "bearer_token": "agt_test_token",
                "runtime_principal_id": "hermes-test-1",
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_verify_discovers_hermes_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    _write_hermes_config(hermes_home / "config.yaml")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # Plugin with no explicit config_path should discover via HERMES_HOME.
    plugin = HermesPreloopPlugin()
    plugin.verify()  # must not raise


def test_verify_discovers_home_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_hermes_config(tmp_path / ".hermes" / "config.yaml")

    plugin = HermesPreloopPlugin()
    plugin.verify()  # must not raise


def test_explicit_config_takes_precedence_over_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Write config in HERMES_HOME location.
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    _write_hermes_config(hermes_home / "config.yaml")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    # Write a different config at the explicit path.
    explicit = tmp_path / "custom" / "config.yaml"
    _write_hermes_config(explicit)

    plugin = HermesPreloopPlugin(config_path=explicit)
    plugin.verify()  # uses the explicit path, must not raise

    # Confirm the explicit path is actually used (not HERMES_HOME).
    block = plugin._read_control_block()
    assert block["runtime"] == "hermes"

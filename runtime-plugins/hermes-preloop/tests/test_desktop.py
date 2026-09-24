"""Desktop capability advertised on the Hermes Agent Control envelope."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the standalone plugin package importable without installation.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from preloop_hermes_plugin.plugin import HermesPreloopPlugin  # noqa: E402

_SECRET = "synthetic-vnc-secret"


def _manifest(path: Path, *, host: str = "127.0.0.1") -> None:
    path.write_text(
        json.dumps(
            {
                "display": ":99",
                "vnc": {
                    "host": host,
                    "port": 5900,
                    "auth": "rfbauth",
                    "password": _SECRET,
                },
                "browser": "chromium",
                "password_file": str(path.parent / "vncpasswd"),
            }
        ),
        encoding="utf-8",
    )
    (path.parent / "vncpasswd").write_text(_SECRET, encoding="utf-8")


def test_capabilities_desktop_vnc_from_manifest(tmp_path, monkeypatch) -> None:
    manifest = tmp_path / "desktop.json"
    _manifest(manifest)
    monkeypatch.setenv("PRELOOP_DESKTOP_FILE", str(manifest))

    caps = HermesPreloopPlugin().capabilities()

    assert caps.desktop == "vnc"
    assert caps.desktop_display == ":99"
    serialized = json.dumps(caps.to_payload())
    assert _SECRET not in serialized
    assert "vncpasswd" not in serialized
    assert "rfbauth" not in serialized
    assert "5900" not in serialized
    assert json.loads(serialized)["desktop"] == "vnc"
    assert json.loads(serialized)["desktop_display"] == ":99"


def test_capabilities_desktop_none_without_manifest(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRELOOP_DESKTOP_FILE", str(tmp_path / "missing.json"))

    caps = HermesPreloopPlugin().capabilities()

    assert caps.desktop == "none"
    assert caps.desktop_display is None
    assert caps.to_payload()["desktop"] == "none"


def test_capabilities_desktop_none_when_host_is_not_loopback(
    tmp_path, monkeypatch
) -> None:
    manifest = tmp_path / "desktop.json"
    _manifest(manifest, host="10.0.0.8")
    monkeypatch.setenv("PRELOOP_DESKTOP_FILE", str(manifest))

    caps = HermesPreloopPlugin().capabilities()

    assert caps.desktop == "none"
    assert caps.desktop_display is None
    assert _SECRET not in json.dumps(caps.to_payload())

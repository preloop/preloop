import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from preloop import server


def test_server_enables_fatal_tracebacks_before_startup() -> None:
    order: list[str] = []
    with (
        patch("faulthandler.enable", side_effect=lambda **_: order.append("faults")),
        patch.object(
            server.preloop_logging,
            "configure_logging",
            side_effect=lambda: order.append("logging"),
        ),
        patch("uvicorn.run", side_effect=lambda *a, **k: order.append("server")),
    ):
        server.start_server(debug=False, init_test_data=False)
    assert order == ["faults", "logging", "server"]


@patch("uvicorn.run")
def test_start_server(
    mock_uvicorn_run: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test server startup without leaking test-data seeding to later tests."""
    # Track the flag so teardown restores it after start_server mutates it.
    monkeypatch.setenv("INIT_TEST_DATA", "false")
    server.start_server(host="127.0.0.1", port=8080, debug=True, init_test_data=True)
    mock_uvicorn_run.assert_called_once_with(
        "preloop.api.app:create_app",
        host="127.0.0.1",
        port=8080,
        reload=True,
        factory=True,
    )


def test_server_enables_fatal_tracebacks_in_spawned_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTHONFAULTHANDLER", raising=False)

    def launch_child(*args: object, **kwargs: object) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import faulthandler; print(faulthandler.is_enabled())",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stdout.strip() == "True"

    with patch("uvicorn.run", side_effect=launch_child):
        server.start_server(debug=True, init_test_data=False)

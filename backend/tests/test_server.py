from unittest.mock import patch
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
def test_start_server(mock_uvicorn_run):
    """
    Tests the start_server function.
    """
    server.start_server(host="127.0.0.1", port=8080, debug=True, init_test_data=True)
    mock_uvicorn_run.assert_called_once_with(
        "preloop.api.app:create_app",
        host="127.0.0.1",
        port=8080,
        reload=True,
        factory=True,
    )

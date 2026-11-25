"""A loopback-only controller endpoint for generated-wrapper tests."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any, Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints.result_validation import router
from preloop.services.result_validation_capability import (
    result_validation_environment,
)


@contextmanager
def validation_server(schema: dict[str, Any]) -> Iterator[dict[str, str]]:
    """Serve the real validation route to the stdlib wrapper subprocess."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - HTTP handler protocol
                result = client.post(
                    self.path,
                    content=self.rfile.read(int(self.headers["Content-Length"])),
                    headers=dict(self.headers),
                )
                self.send_response(result.status_code)
                self.end_headers()
                self.wfile.write(result.content)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            env = result_validation_environment(schema)
            env["PRELOOP_RESULT_VALIDATION_URL"] = (
                f"http://127.0.0.1:{server.server_port}/api/v1/internal/result-validation"
            )
            yield env
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

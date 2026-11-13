"""Generic fixture site for browser QA flows.

Serves a sign-in page, a login form with synthetic credentials, a page that
only renders after login, and three boundary routes: a redirect to the cloud
metadata address, a redirect to an off-allowlist origin, and a slow page.
Standard library only, so the image needs nothing beyond Python.
"""

from __future__ import annotations

import html
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

FIXTURE_USER = "jane@example.com"
FIXTURE_PASSWORD = "fixture-pass"
FIXTURE_DISPLAY_NAME = "Jane Doe"
SESSION_COOKIE = "fixture_session"
SESSION_VALUE = "signed-in"
ESCAPE_TARGET = "http://169.254.169.254/latest/meta-data/"
OFFSITE_TARGET = "https://example.org/"
MAX_FORM_BYTES = 4096


def _page(title: str, body: str) -> bytes:
    """Render a minimal HTML page.

    Args:
        title: Text for the ``<title>`` element; escaped here.
        body: Trusted HTML for the ``<body>`` element.

    Returns:
        The encoded document.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
    ).encode()


LOGIN_FORM = (
    "<h1>Sign in</h1>"
    "<form method='post' action='/login'>"
    "<label>Email <input name='email' type='email' autocomplete='off'></label>"
    "<label>Password <input name='password' type='password'"
    " autocomplete='off'></label>"
    "<button type='submit'>Log in</button>"
    "</form>"
)


class FixtureHandler(BaseHTTPRequestHandler):
    """Route handler for the fixture site."""

    server_version = "preloop-fixture-site"
    sys_version = ""
    slow_seconds: float = 30.0

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Log one line per request without the request body."""
        print(
            f"fixture-site {self.command} {self.path.split('?', 1)[0]}"
            f" {args[1] if len(args) > 1 else ''}",
            flush=True,
        )

    def _send(
        self,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if body:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _signed_in(self) -> bool:
        cookies = self.headers.get("Cookie", "")
        return any(
            part.strip() == f"{SESSION_COOKIE}={SESSION_VALUE}"
            for part in cookies.split(";")
        )

    def do_GET(self) -> None:  # noqa: N802
        """Serve the GET routes."""
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(
                200,
                _page(
                    "Fixture home", "<h1>Fixture site</h1><a href='/login'>Sign in</a>"
                ),
            )
        elif path == "/login":
            self._send(200, _page("Sign in", LOGIN_FORM))
        elif path == "/welcome":
            if not self._signed_in():
                self._send(303, headers={"Location": "/login"})
                return
            name = html.escape(FIXTURE_DISPLAY_NAME)
            self._send(200, _page("Welcome", f"<h1>Welcome, {name}</h1>"))
        elif path == "/escape":
            self._send(302, headers={"Location": ESCAPE_TARGET})
        elif path == "/offsite":
            self._send(302, headers={"Location": OFFSITE_TARGET})
        elif path == "/slow":
            time.sleep(self.slow_seconds)
            self._send(200, _page("Slow", "<p>Slow page</p>"))
        elif path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self._send(404, _page("Not found", "<p>Not found</p>"))

    def do_POST(self) -> None:  # noqa: N802
        """Check the synthetic credentials and start a session."""
        if self.path.split("?", 1)[0] != "/login":
            self._send(404, _page("Not found", "<p>Not found</p>"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_FORM_BYTES:
            self._send(413, _page("Too large", "<p>Form too large</p>"))
            return
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        email = form.get("email", [""])[0]
        password = form.get("password", [""])[0]
        if email == FIXTURE_USER and password == FIXTURE_PASSWORD:
            self._send(
                303,
                headers={
                    "Location": "/welcome",
                    "Set-Cookie": (
                        f"{SESSION_COOKIE}={SESSION_VALUE}; Path=/; HttpOnly;"
                        " SameSite=Lax"
                    ),
                },
            )
            return
        self._send(
            401,
            _page("Sign in", "<p role='alert'>Invalid credentials</p>" + LOGIN_FORM),
        )


def make_server(
    host: str, port: int, slow_seconds: float = 30.0
) -> ThreadingHTTPServer:
    """Build a threaded fixture server.

    Args:
        host: Address to bind.
        port: TCP port to bind; ``0`` picks a free port.
        slow_seconds: How long ``/slow`` sleeps before answering.

    Returns:
        The bound, not yet serving, server.
    """
    handler = type(
        "BoundFixtureHandler", (FixtureHandler,), {"slow_seconds": slow_seconds}
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    """Serve until interrupted, configured by ``FIXTURE_*`` variables."""
    server = make_server(
        os.environ.get("FIXTURE_HOST", "0.0.0.0"),  # noqa: S104 # nosec B104
        int(os.environ.get("FIXTURE_PORT", "8080")),
        float(os.environ.get("FIXTURE_SLOW_SECONDS", "30")),
    )
    print(f"fixture-site listening {server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

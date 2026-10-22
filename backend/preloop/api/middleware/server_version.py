"""Response-only software release diagnostics for the REST API."""

from starlette._utils import get_route_path
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from preloop.config import SERVER_VERSION


class ServerVersionMiddleware:
    """Add the release header without buffering responses or reading requests.

    This must wrap FastAPI's complete middleware stack, including its outer
    ServerErrorMiddleware, so unhandled errors carry the same diagnostics.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = get_route_path(scope)
        if path != "/api/v1" and not path.startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Preloop-Server-Version"] = SERVER_VERSION
            await send(message)

        await self.app(scope, receive, send_with_version)

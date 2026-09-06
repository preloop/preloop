"""LOCAL TEST IMAGE ONLY: intercept provider/Git network, preserve real Git IO."""

import json
from typing import Any
import httpx
from preloop.services.trusted_publisher import CleanGitRepository

real_run = CleanGitRepository.run


def fake_network_git(self: CleanGitRepository, *args: str, **kwargs: Any) -> str:
    if args[0] == "ls-remote":
        return ""
    if args[0] == "push":
        return ""
    if args[0] == "fsck":
        self._fixture_head = args[-1]
    if args[0] == "fetch":
        base = real_run(self, "rev-parse", self._fixture_head + "~1")
        real_run(self, "update-ref", "refs/preloop/base", base)
        return ""
    return real_run(self, *args, **kwargs)


CleanGitRepository.run = fake_network_git


def fake_provider(request: httpx.Request) -> httpx.Response:
    if request.url.host != "api.github.com":
        raise RuntimeError("Non-fixture provider forbidden")
    if request.method == "DELETE" and request.url.path == "/installation/token":
        return httpx.Response(204)
    if request.url.path == "/repos/example/project/pulls":
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.method == "POST":
            payload = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "number": 1,
                    "html_url": "https://github.com/example/project/pull/1",
                    "body": payload["body"],
                },
            )
    if (
        request.method == "PATCH"
        and request.url.path == "/repos/example/project/pulls/1"
    ):
        return httpx.Response(200, json={})
    raise RuntimeError("Unexpected fixture provider request")


class FixtureClient(httpx.AsyncClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(fake_provider)
        super().__init__(*args, **kwargs)


httpx.AsyncClient = FixtureClient

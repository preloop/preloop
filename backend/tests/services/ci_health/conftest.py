"""A fixture-backed fake of the GitHub Actions and GitLab CI REST APIs.

Responses are the recorded provider shapes under tests/fixtures/ci_health,
with synthetic ids and names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest

from preloop.services.ci_health import providers
from preloop.services.ci_health.providers import GitHubActions, GitLabCi
from preloop.services.ci_health.transport import Budget, HttpTransport

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ci_health"
GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.example.com/api/v4"
GITLAB_PROJECT = "/api/v4/projects/example-group%2Fsub%2Fsvc"


def load(name: str) -> Any:
    """Read a JSON fixture."""
    return json.loads((FIXTURES / name).read_text())


class FakeCi:
    """Routes recorded responses by path and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.headers: dict[str, str] = {"x-ratelimit-remaining": "4000"}
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.pulls: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw = request.url.raw_path.decode().split("?")[0]
        for pattern, override in self.overrides.items():
            if re.search(pattern, raw):
                return override(request)
        body = self._route(request, raw)
        if isinstance(body, httpx.Response):
            return body
        if body is None:
            return httpx.Response(
                404, json={"message": "Not Found"}, headers=self.headers
            )
        if isinstance(body, str):
            return httpx.Response(200, text=body, headers=self.headers)
        return httpx.Response(200, json=body, headers=self.headers)

    def paths(self) -> list[str]:
        """Requested paths, in order."""
        return [r.url.raw_path.decode().split("?")[0] for r in self.requests]

    def _route(self, request: httpx.Request, raw: str) -> Optional[Any]:
        if request.url.host == "logs.example.net":
            job = raw.rsplit("/", 1)[-1]
            return (FIXTURES / "github" / f"log_{job}.txt").read_text()
        if raw == "/repos/example-org/app":
            return load("github/repo.json")
        if raw == "/repos/example-org/app/actions/runs":
            return load("github/runs.json")
        if match := re.fullmatch(
            r"/repos/example-org/app/actions/runs/(\d+)/jobs", raw
        ):
            return load(f"github/jobs_{match[1]}.json")
        if match := re.fullmatch(
            r"/repos/example-org/app/actions/jobs/(\d+)/logs", raw
        ):
            if not (FIXTURES / "github" / f"log_{match[1]}.txt").exists():
                return None
            # GitHub answers with a redirect to short-lived blob storage.
            return httpx.Response(
                302, headers={"location": f"https://logs.example.net/blob/{match[1]}"}
            )
        if match := re.fullmatch(r"/repos/example-org/app/pulls/(\d+)", raw):
            return self.pulls.get(match[1])
        project = re.escape(GITLAB_PROJECT)
        if raw == GITLAB_PROJECT:
            return load("gitlab/project.json")
        if raw == f"{GITLAB_PROJECT}/pipelines":
            return load("gitlab/pipelines.json")
        if match := re.fullmatch(project + r"/pipelines/(\d+)/jobs", raw):
            return load(f"gitlab/jobs_{match[1]}.json")
        if match := re.fullmatch(project + r"/pipelines/(\d+)/test_report", raw):
            report = FIXTURES / "gitlab" / f"test_report_{match[1]}.json"
            return json.loads(report.read_text()) if report.exists() else None
        if match := re.fullmatch(project + r"/jobs/(\d+)/trace", raw):
            trace = FIXTURES / "gitlab" / f"trace_{match[1]}.txt"
            return trace.read_text() if trace.exists() else None
        if match := re.fullmatch(project + r"/merge_requests/(\d+)", raw):
            return self.pulls.get(match[1])
        return None


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    providers.JOB_CACHE.clear()
    providers.FINDINGS_CACHE.clear()


@pytest.fixture
def fake_ci() -> FakeCi:
    return FakeCi()


def make_providers(fake: FakeCi, budget: Budget) -> tuple[GitHubActions, GitLabCi]:
    """Both adapters on one mocked client sharing ``budget``."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))

    async def gh_headers() -> dict[str, str]:
        return {"Authorization": "token test-token"}

    async def gl_headers() -> dict[str, str]:
        return {"PRIVATE-TOKEN": "test-token"}

    return (
        GitHubActions(HttpTransport(GITHUB_API, gh_headers, budget, client=client)),
        GitLabCi(HttpTransport(GITLAB_API, gl_headers, budget, client=client)),
    )

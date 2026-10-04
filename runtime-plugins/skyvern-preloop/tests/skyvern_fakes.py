"""Recorded Skyvern API v1 responses served through httpx.MockTransport."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

FIXTURE = Path(__file__).parent / "fixtures" / "skyvern_task_recorded.json"
SKYVERN = "https://skyvern.example.test"


def recorded() -> dict:
    return json.loads(FIXTURE.read_text())


class RecordedSkyvern:
    """Answers the v1 task routes and signed artifact URLs from the fixture."""

    def __init__(self, data: dict | None = None):
        self.data = data or recorded()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.data["downloads"]:
            return httpx.Response(
                200, content=base64.b64decode(self.data["downloads"][url])
            )
        if request.headers.get("x-api-key") != "sk-skyvern-test":
            return httpx.Response(403)
        task_id = self.data["task"]["task_id"]
        base = f"{SKYVERN}/api/v1/tasks/{task_id}"
        if url == base:
            return httpx.Response(200, json=self.data["task"])
        if url == f"{base}/steps":
            return httpx.Response(200, json=list(reversed(self.data["steps"])))
        prefix = f"{base}/steps/"
        if url.startswith(prefix) and url.endswith("/artifacts"):
            step_id = url[len(prefix) : -len("/artifacts")]
            return httpx.Response(200, json=self.data["artifacts"].get(step_id, []))
        return httpx.Response(404, json={"detail": "Not Found"})


class FakePreloop:
    """In-memory stand-in for the browser step and deposit routes."""

    def __init__(self, *, deposit_route: bool = True):
        self.steps: dict[str, dict] = {}
        self.artifacts: dict[str, dict] = {}
        self.deposit_route = deposit_route
        self.auth: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        path = request.url.path
        if path.endswith("/browser-steps"):
            accepted = duplicates = 0
            for step in json.loads(request.content)["steps"]:
                key = f"{step['source']}:{step['source_step_id']}"
                if key in self.steps:
                    duplicates += 1
                else:
                    self.steps[key] = step
                    accepted += 1
            return httpx.Response(
                200,
                json={"accepted": accepted, "duplicates": duplicates, "rejected": []},
            )
        if path.endswith("/artifacts") and self.deposit_route:
            key = request.headers["idempotency-key"]
            if key not in self.artifacts:
                body = request.content
                meta = json.loads(
                    body.split(b'name="metadata"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0]
                )
                self.artifacts[key] = {
                    "id": f"art-{len(self.artifacts) + 1}",
                    "kind": meta["kind"],
                    "name": meta["name"],
                    "body": body,
                }
            return httpx.Response(201, json=self.artifacts[key] | {"body": None})
        return httpx.Response(404, json={"detail": "Not Found"})

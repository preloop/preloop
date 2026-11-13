"""Check that two CI identities sharing one review flow cannot see each other.

Run this against your own Preloop URL with the CI tokens of two identities
that are bound to two different projects on the same shared read-only review
flow. It starts no review and changes nothing: every request is a read, or a
trigger that is malformed on purpose and is refused before any execution is
created.

    PRELOOP_CI_TOKEN_A=... PRELOOP_CI_TOKEN_B=... python \
        scripts/verify_shared_ci_flow.py --url https://preloop.example \
        --flow FLOW_ID --project-a PROJECT_A_ID --project-b PROJECT_B_ID

Tokens are read from the environment only and are never printed. The exit
status is 0 when every check passed and 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

API = "/api/v1"
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: ``request(token, method, path, body) -> (status, parsed JSON or None)``.
Request = Callable[[str, str, str, "dict[str, Any] | None"], "tuple[int, Any]"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect must not forward the CI token to another origin."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http_request(base_url: str) -> Request:
    """Build the real transport for one Preloop origin.

    Args:
        base_url: HTTPS origin, or HTTP on a loopback host.

    Returns:
        A request function that sends one bearer-authenticated call.

    Raises:
        SystemExit: The URL is not HTTPS (or loopback HTTP) or carries
            credentials, a query or a fragment.
    """
    origin = urlsplit(base_url)
    loopback = origin.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        origin.scheme not in {"https", "http"}
        or (origin.scheme == "http" and not loopback)
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
    ):
        raise SystemExit("--url must be an HTTPS origin without credentials")
    root = base_url.rstrip("/")
    opener = urllib.request.build_opener(_NoRedirect)

    def request(
        token: str, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        call = urllib.request.Request(  # noqa: S310 - scheme checked above
            root + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with opener.open(call, timeout=30) as response:  # noqa: S310
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None

    return request


def run_checks(
    request: Request,
    *,
    flow: str,
    tokens: dict[str, str],
    projects: dict[str, str],
) -> list[tuple[bool, str]]:
    """Run every sideways-access check for two identities on one flow.

    Args:
        request: Transport, see :data:`Request`.
        flow: The shared flow id.
        tokens: CI tokens by identity label (``"A"`` and ``"B"``).
        projects: The project id each identity is bound to, by label.

    Returns:
        ``(passed, description)`` for each check, in order. Descriptions
        never contain a token.
    """
    results: list[tuple[bool, str]] = []

    def check(passed: bool, description: str) -> None:
        results.append((bool(passed), description))

    owned: dict[str, list[str]] = {}
    for label, token in tokens.items():
        other = next(name for name in tokens if name != label)
        status, rows = request(token, "GET", f"{API}/flows/executions?limit=100", None)
        listed = rows if status == 200 and isinstance(rows, list) else []
        check(status == 200, f"{label}: lists its executions (HTTP {status})")
        check(
            all(
                isinstance(row, dict)
                and row.get("flow_id") == flow
                and row.get("project_id") == projects[label]
                for row in listed
            ),
            f"{label}: every listed execution is on the shared flow and its "
            f"own project ({len(listed)} rows)",
        )
        check(
            projects[other] not in json.dumps(rows),
            f"{label}: the listing never names project {other}",
        )
        receipts = [
            (row, row["startup_receipt"])
            for row in listed
            if isinstance(row, dict) and isinstance(row.get("startup_receipt"), dict)
        ]
        check(
            all(
                receipt.get("commit") == row.get("head_sha")
                and receipt.get("checkout_verified") is True
                for row, receipt in receipts
            ),
            f"{label}: every startup receipt names the admitted head commit "
            f"({len(receipts)} receipts)",
        )
        owned[label] = [
            str(row["id"]) for row in listed if isinstance(row, dict) and row.get("id")
        ]
        for path, what in (
            (f"{API}/ci-identities/shared-flows/{flow}", "the flow's allowlist"),
            (f"{API}/ci-identities", "the identity list"),
            (f"{API}/flows/{flow}", "the flow configuration"),
        ):
            status, body = request(token, "GET", path, None)
            check(
                status in {401, 403} and projects[other] not in json.dumps(body),
                f"{label}: cannot read {what} (HTTP {status})",
            )
        status, _ = request(
            token,
            "POST",
            f"{API}/flows/{flow}/trigger",
            {
                "pr_number": 1,
                "head_sha": "0" * 40,
                "project_id": projects[other],
            },
        )
        check(
            status in {403, 422},
            f"{label}: a trigger naming project {other} is refused before any "
            f"execution exists (HTTP {status})",
        )
    for label, token in tokens.items():
        other = next(name for name in tokens if name != label)
        if not owned[other]:
            check(
                True,
                f"{label}: no execution of {other} to probe yet; run one "
                "review per repository and repeat for the cross-read checks",
            )
            continue
        for execution in owned[other][:5]:
            for suffix in ("", "/result", "/tool-evidence"):
                status, _ = request(
                    token, "GET", f"{API}/flows/executions/{execution}{suffix}", None
                )
                check(
                    status in {403, 404},
                    f"{label}: cannot read {other}'s execution{suffix or ''} "
                    f"(HTTP {status})",
                )
    return results


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run the checks and print one line per check.

    Args:
        argv: Arguments, defaulting to the process arguments.

    Returns:
        0 when every check passed, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--url", required=True, help="Preloop origin")
    parser.add_argument("--flow", required=True, help="shared flow id")
    parser.add_argument("--project-a", required=True, help="project of identity A")
    parser.add_argument("--project-b", required=True, help="project of identity B")
    arguments = parser.parse_args(argv)
    identifiers = (arguments.flow, arguments.project_a, arguments.project_b)
    if not all(_UUID.match(value) for value in identifiers):
        raise SystemExit("--flow, --project-a and --project-b must be UUIDs")
    if arguments.project_a.lower() == arguments.project_b.lower():
        raise SystemExit("The two identities must be bound to different projects")
    tokens = {
        "A": os.environ.get("PRELOOP_CI_TOKEN_A", ""),
        "B": os.environ.get("PRELOOP_CI_TOKEN_B", ""),
    }
    if not all(tokens.values()) or tokens["A"] == tokens["B"]:
        raise SystemExit(
            "Set PRELOOP_CI_TOKEN_A and PRELOOP_CI_TOKEN_B to two different CI tokens"
        )
    results = run_checks(
        http_request(arguments.url),
        flow=arguments.flow.lower(),
        tokens=tokens,
        projects={
            "A": arguments.project_a.lower(),
            "B": arguments.project_b.lower(),
        },
    )
    for passed, description in results:
        print(("PASS  " if passed else "FAIL  ") + description)
    failed = sum(1 for passed, _ in results if not passed)
    print(f"{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

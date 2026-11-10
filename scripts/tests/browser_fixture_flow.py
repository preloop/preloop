"""Model-free browser QA flow against the generic fixture site.

Drives the Playwright MCP server that ``environments/preloop/browser/enable.sh``
registered for the harness, over MCP stdio, with no model in the loop:

1. Navigate to the fixture home page, click "Sign in", type the synthetic
   credentials, submit, and assert "Welcome, Jane Doe" is visible.
2. Boundary cases, one ``notes`` line each: a non-allowlisted origin, a
   redirect to the metadata address, a navigation that exceeds the tool
   timeout (the server must still answer afterwards), and cleanup (no
   Chromium process left once the MCP server is closed).
3. Write ``result.json``: ``url``, ``pass``, at most five short ``notes`` and
   ``checked_at``. The file is capped at 4 KiB and credentials are redacted.

Origin enforcement is the egress proxy's job. This script only observes what
the browser got back; the controller that owns the proxy log checks that the
proxy denied the requests (:func:`unexpected_denials`).

Standard library plus the ``mcp`` package, so it runs inside the sandbox with
``/opt/preloop-tests/bin/python``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

FIXTURE_USER = "jane@example.com"
FIXTURE_PASSWORD = "fixture-pass"  # nosec B105 - synthetic fixture credential
WELCOME_TEXT = "Welcome, Jane Doe"
OFFSITE_URL = "https://example.org/"
METADATA_HOST = "169.254.169.254"
RESULT_MAX_BYTES = 4096
MAX_NOTES = 5
MAX_NOTE_CHARS = 160
REDACTED = "[redacted]"
PROXY_ERROR = re.compile(
    r"ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|egress_denied"
)
# Text that would only appear if the browser reached a metadata service.
METADATA_CONTENT = re.compile(r"ami-id|instance-id|iam/security-credentials")


@dataclass(frozen=True)
class McpServerCommand:
    """The stdio command line a harness uses to start the browser MCP."""

    command: str
    args: list[str]


@dataclass
class FlowOutcome:
    """What the scenario observed, before it is bounded into ``result.json``."""

    url: str = ""
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether every step and boundary case behaved as specified."""
        return not self.failures


def load_server_command(config_path: Path) -> McpServerCommand:
    """Read the ``browser`` MCP server that ``enable.sh`` registered.

    Args:
        config_path: Codex ``config.toml`` or Claude ``.mcp.json``.

    Returns:
        The command and arguments, exactly as the harness would run them.

    Raises:
        ValueError: The file has no ``browser`` server entry.
    """
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix == ".toml":
        entry = tomllib.loads(text).get("mcp_servers", {}).get("browser")
    else:
        entry = json.loads(text).get("mcpServers", {}).get("browser")
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        raise ValueError(f"no browser MCP server in {config_path}")
    args = entry.get("args") or []
    if not all(isinstance(item, str) for item in args):
        raise ValueError(f"browser MCP args are not strings in {config_path}")
    return McpServerCommand(command=entry["command"], args=list(args))


def redact(text: str, secrets: tuple[str, ...] = (FIXTURE_PASSWORD,)) -> str:
    """Replace every secret occurrence in ``text``.

    Args:
        text: Free text that may echo typed input.
        secrets: Values that must never be written out.

    Returns:
        The text with each secret replaced by ``[redacted]``.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def bounded_result(
    url: str,
    passed: bool,
    notes: list[str],
    checked_at: str,
    *,
    secrets: tuple[str, ...] = (FIXTURE_PASSWORD,),
) -> dict[str, Any]:
    """Build the ``result.json`` document within the size cap.

    Notes are redacted, cut to :data:`MAX_NOTE_CHARS`, and limited to
    :data:`MAX_NOTES`. If the serialized document is still over
    :data:`RESULT_MAX_BYTES`, notes are dropped from the end and the URL is
    shortened until it fits.

    Args:
        url: Final page URL of the scenario.
        passed: Overall outcome.
        notes: One short line per check.
        checked_at: ISO 8601 timestamp.
        secrets: Values to redact.

    Returns:
        A JSON-serializable dict whose UTF-8 encoding is at most 4 KiB.
    """
    kept = [redact(str(note), secrets)[:MAX_NOTE_CHARS] for note in notes[:MAX_NOTES]]
    doc: dict[str, Any] = {
        "url": redact(url, secrets)[:2048],
        "pass": bool(passed),
        "notes": kept,
        "checked_at": checked_at[:64],
    }
    while len(serialize_result(doc)) > RESULT_MAX_BYTES:
        if doc["notes"]:
            doc["notes"].pop()
        else:
            doc["url"] = doc["url"][: len(doc["url"]) // 2]
    return doc


def serialize_result(doc: dict[str, Any]) -> bytes:
    """Encode a result document the way it is written to disk."""
    return (json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n").encode()


def write_result(
    path: Path, outcome: FlowOutcome, checked_at: str | None = None
) -> dict[str, Any]:
    """Write the bounded, redacted ``result.json``.

    Args:
        path: Destination file.
        outcome: Scenario observations.
        checked_at: Timestamp override; defaults to now in UTC.

    Returns:
        The document that was written.
    """
    doc = bounded_result(
        outcome.url,
        outcome.passed,
        outcome.notes,
        checked_at or datetime.now(UTC).isoformat(timespec="seconds"),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize_result(doc))
    return doc


def _denied_host(target: str) -> str:
    if "://" in target:
        return (urlsplit(target).hostname or "").lower()
    return target.rsplit(":", 1)[0].strip("[]").lower()


def _parse_ts(value: str) -> datetime | None:
    try:
        # The proxy writes nanosecond precision; datetime keeps microseconds.
        trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


def proxy_denials(
    log_text: str,
    *,
    since: datetime | None = None,
    allowed_dial_hosts: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    """Extract ``(host, reason)`` for every denial in an egress proxy log.

    Args:
        log_text: Proxy stdout, one JSON object per line.
        since: Skip entries logged before this instant (for example the
            setup self-check, which runs before the flow).
        allowed_dial_hosts: Hosts whose ``dial_error`` lines are expected.
            ``dial_error`` means the request was allowed and then failed
            upstream; the abandoned ``/slow`` request to the fixture is one.
            For any other host it is reported, since it means the host got
            past the allowlist.

    Returns:
        Denials in log order. Non-JSON lines and allowed entries are skipped.
    """
    denials = []
    for line in log_text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or not entry.get("reason"):
            continue
        if not entry.get("target"):
            continue
        if since is not None:
            logged = _parse_ts(str(entry.get("ts", "")))
            if logged is None or logged < since:
                continue
        host = _denied_host(str(entry["target"]))
        if entry["reason"] == "dial_error" and host in allowed_dial_hosts:
            continue
        denials.append((host, str(entry["reason"])))
    return denials


def unexpected_denials(
    log_text: str,
    *,
    expected_hosts: tuple[str, ...] = ("example.org", METADATA_HOST),
    ignored_hosts: tuple[str, ...] = (),
    since: datetime | None = None,
    allowed_dial_hosts: tuple[str, ...] = (),
) -> tuple[list[str], list[tuple[str, str]]]:
    """Compare the proxy's denials with the ones the scenario provokes.

    Args:
        log_text: Proxy stdout covering the scenario.
        expected_hosts: Hosts that must each have at least one denial.
        ignored_hosts: Hosts denied by something other than this scenario
            (for example the setup self-check's loopback probe).
        since: Only count entries logged at or after this instant.
        allowed_dial_hosts: See :func:`proxy_denials`.

    Returns:
        ``(missing, extra)``: expected hosts with no denial, and denials of
        hosts that were neither expected nor ignored.
    """
    denials = proxy_denials(
        log_text, since=since, allowed_dial_hosts=allowed_dial_hosts
    )
    # dial_error means the proxy allowed the request; it never counts as the
    # expected denial and is always reported, even for an expected host.
    hosts = {host for host, reason in denials if reason != "dial_error"}
    missing = [host for host in expected_hosts if host not in hosts]
    extra = [
        (host, reason)
        for host, reason in denials
        if reason == "dial_error"
        or (host not in expected_hosts and host not in ignored_hosts)
    ]
    return missing, extra


def chromium_pids() -> set[int]:
    """PIDs whose command line matches ``chrom`` (``pgrep -f chrom``)."""
    try:
        found = subprocess.run(  # nosec B603 B607 - fixed argv
            ["pgrep", "-f", "chrom"], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return _proc_scan("chrom")
    own = os.getpid()
    return {
        int(pid) for pid in found.stdout.split() if pid.isdigit() and int(pid) != own
    }


def _proc_scan(needle: str) -> set[int]:
    pids = set()
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if needle.encode() in cmdline and int(entry.name) != os.getpid():
            pids.add(int(entry.name))
    return pids


def _text(result: Any) -> str:
    return "\n".join(
        getattr(item, "text", "") for item in getattr(result, "content", []) or []
    )


def _ref(snapshot: str, role: str, name: str) -> str | None:
    match = re.search(
        rf'{re.escape(role)} "{re.escape(name)}"[^\n]*\[ref=([A-Za-z0-9_-]+)\]',
        snapshot,
    )
    return match.group(1) if match else None


def _page_url(text: str) -> str:
    match = re.search(r"Page URL: (\S+)", text)
    return match.group(1) if match else ""


ToolCall = Callable[[str, dict[str, Any]], Awaitable[tuple[bool, str]]]


async def _login(call: ToolCall, base: str) -> tuple[str, str | None]:
    """Sign in through the UI the way a user would.

    Args:
        call: Invokes one MCP tool and returns ``(is_error, text)``.
        base: Fixture origin without a trailing slash.

    Returns:
        ``(final_url, problem)``; ``problem`` is ``None`` on success.
    """
    is_error, _ = await call("browser_navigate", {"url": base + "/"})
    if is_error:
        return "", "home page did not load"
    _, snap = await call("browser_snapshot", {})
    link = _ref(snap, "link", "Sign in")
    if "Sign in" not in snap or not link:
        return _page_url(snap), "no 'Sign in' link"
    is_error, _ = await call("browser_click", {"element": "Sign in", "target": link})
    if is_error:
        return _page_url(snap), "click on 'Sign in' failed"
    _, snap = await call("browser_snapshot", {})
    email = _ref(snap, "textbox", "Email")
    password = _ref(snap, "textbox", "Password")
    if not email or not password:
        return _page_url(snap), "login form not found"
    for label, ref, text, submit in (
        ("Email", email, FIXTURE_USER, False),
        ("Password", password, FIXTURE_PASSWORD, True),
    ):
        is_error, _ = await call(
            "browser_type",
            {"element": label, "target": ref, "text": text, "submit": submit},
        )
        if is_error:
            return _page_url(snap), f"typing into {label} failed"
    _, snap = await call("browser_snapshot", {})
    url = _page_url(snap)
    if WELCOME_TEXT not in snap or urlsplit(url).path != "/welcome":
        return url, "welcome text not visible"
    return url, None


async def run_scenario(
    server: McpServerCommand,
    base_url: str,
    *,
    tool_timeout: float = 5.0,
    step_timeout: float = 60.0,
) -> FlowOutcome:
    """Run the login scenario and the four boundary cases.

    Args:
        server: Browser MCP command line from the harness config.
        base_url: Fixture site origin, for example ``http://fixture-site:8080``.
        tool_timeout: Client timeout for the ``/slow`` navigation.
        step_timeout: Client timeout for every other tool call.

    Returns:
        The observations; ``failures`` is empty when everything passed.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    outcome = FlowOutcome()
    base = base_url.rstrip("/")
    baseline = chromium_pids()
    params = StdioServerParameters(
        command=server.command, args=server.args, env=dict(os.environ)
    )

    def fail(step: str, detail: str = "") -> None:
        outcome.failures.append(step)
        outcome.notes.append(f"{step}: FAIL {detail}".strip())

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(
                name: str, args: dict[str, Any], timeout: float = step_timeout
            ) -> tuple[bool, str]:
                result = await session.call_tool(
                    name, args, read_timeout_seconds=timeout
                )
                # mcp 2.x names the flag is_error; 1.x used isError.
                is_error = getattr(result, "is_error", None)
                if is_error is None:
                    is_error = getattr(result, "isError", False)
                return bool(is_error), _text(result)

            # Permitted login.
            url, problem = await _login(call, base)
            outcome.url = url
            if problem:
                fail("login", problem)
            else:
                outcome.notes.append(f"login: ok, saw '{WELCOME_TEXT}'")

            # Rejected origin: the proxy refuses the CONNECT tunnel.
            is_error, text = await call("browser_navigate", {"url": OFFSITE_URL})
            if is_error and PROXY_ERROR.search(text):
                outcome.notes.append(
                    "rejected_origin: ok, " + PROXY_ERROR.search(text).group(0)  # type: ignore[union-attr]
                )
            else:
                fail("rejected_origin", "navigation was not refused by the proxy")

            # Redirect escape: /escape answers 302 to the metadata address.
            is_error, text = await call("browser_navigate", {"url": base + "/escape"})
            _, snap = await call("browser_snapshot", {})
            combined = text + "\n" + snap
            if METADATA_CONTENT.search(combined):
                fail("redirect_escape", "metadata content reached the page")
            elif PROXY_ERROR.search(combined) or "chrome-error://" in combined:
                outcome.notes.append("redirect_escape: ok, metadata request denied")
            else:
                fail("redirect_escape", "no proxy denial visible")

            # Timeout: the client gives up, the server keeps answering.
            timed_out = False
            started = time.monotonic()
            try:
                await call(
                    "browser_navigate", {"url": base + "/slow"}, timeout=tool_timeout
                )
            except Exception as exc:  # McpError on read timeout
                timed_out = (
                    "timed out" in str(exc).lower() or "timeout" in str(exc).lower()
                )
            elapsed = time.monotonic() - started
            responsive = False
            if timed_out:
                is_error, text = await call(
                    "browser_navigate", {"url": base + "/login"}
                )
                responsive = not is_error and "Sign in" in text
            if timed_out and responsive:
                outcome.notes.append(
                    f"timeout: ok, gave up after {elapsed:.1f}s, server responsive"
                )
            else:
                fail("timeout", f"timed_out={timed_out} responsive={responsive}")

    # Cleanup: the MCP server is closed; its Chromium must be gone too.
    leaked: set[int] = set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        leaked = chromium_pids() - baseline
        if not leaked:
            break
        await asyncio.sleep(0.5)
    if leaked:
        fail("cleanup", f"{len(leaked)} chromium processes left")
    else:
        outcome.notes.append("cleanup: ok, no chromium process after close")
    return outcome


def main(argv: list[str] | None = None) -> int:
    """Run the flow and write ``result.json``; exit 0 only on ``pass: true``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mcp-config",
        type=Path,
        default=Path.home() / ".codex" / "config.toml",
        help="harness config written by enable.sh",
    )
    parser.add_argument("--base-url", default="http://fixture-site:8080")
    parser.add_argument("--result", type=Path, default=Path("result.json"))
    parser.add_argument("--tool-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    server = load_server_command(args.mcp_config)
    outcome = asyncio.run(
        run_scenario(server, args.base_url, tool_timeout=args.tool_timeout)
    )
    doc = write_result(args.result, outcome)
    print(
        "BROWSER_FIXTURE_RESULT " + serialize_result(doc).decode().strip(), flush=True
    )
    return 0 if doc["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

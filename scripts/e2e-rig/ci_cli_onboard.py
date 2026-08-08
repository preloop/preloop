#!/usr/bin/env python3
"""Headless CLI-onboarding e2e check for GitLab CI (test:integration:cli-onboard).

The CI twin of `modules/08-cli-onboard.py`. Module 08 needs a VM, an ssh
transport and the pexpect+agg recording toolchain — none of which exist in a
CI job container — so this entrypoint reuses module 08's *logic* through
`lib/cli_onboard.py` (login persistence, agents-list parsing, gateway env
extraction) and swaps only the transport: subprocess instead of a recorded pty,
localhost instead of ssh. `lib/riglib.py` supplies the HTTP/redaction helpers
so failure output here is redacted exactly like the rig's.

What it asserts, in order:
  1. a token login persisted by the CLI's own config path verifies server-side
     (`preloop auth status`);
  2. `preloop agents onboard --yes` enrolls a planted Claude Code install in
     API-key mode, non-interactively, with no prompt left unanswered;
  3. the enrollment reports gateway routing and wrote a durable credential
     into the agent's settings;
  4. a request made with that credential is accepted by the Preloop gateway
     (not 401/403) and lands as a usage row on the account.

Environment:
  PRELOOP_TEST_URL      base URL of the deployed test environment
  PRELOOP_TEST_API_KEY  API key for an account in that environment
Exit codes: 0 pass, non-zero fail (no skip path — CI runs this deliberately).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import cli_onboard  # noqa: E402
import riglib  # noqa: E402

URL = os.environ.get("PRELOOP_TEST_URL", "").rstrip("/")
TOKEN = os.environ.get("PRELOOP_TEST_API_KEY", "")
PRELOOP_BIN = os.environ.get("PRELOOP_BIN", "preloop")
HOME = Path(os.environ["HOME"])
ARTIFACT_DIR = Path(os.environ.get("CLI_ONBOARD_ARTIFACTS", "cli-onboard-artifacts"))

# Upstream Anthropic never sees this: onboarding replaces it with the durable
# Preloop credential. Its only job is to put the install in API-key mode so
# no OAuth/subscription lineage is exercised (module 08 covers that path on a
# real machine).
PLACEHOLDER_UPSTREAM_KEY = "sk-ant-ci-placeholder-not-a-real-key"

ANTHROPIC_VERSION = "2023-06-01"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", flush=True)
    sys.exit(1)


def save_artifact(name: str, content: str) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / name).write_text(riglib.redact(content, limit=20000))


def cli(*args: str, check: bool = True, timeout: int = 900) -> str:
    """Run the CLI non-interactively with telemetry off.

    stdin is closed: any prompt that --yes fails to cover must surface as a
    failure here rather than hanging the job until its timeout.
    """
    env = dict(os.environ)
    env["PRELOOP_DISABLE_TELEMETRY"] = "true"
    printable = " ".join([PRELOOP_BIN, *args])
    print(f"$ {printable}", flush=True)
    proc = subprocess.run(
        [PRELOOP_BIN, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    output = proc.stdout + proc.stderr
    print(riglib.redact(output, limit=20000), flush=True)
    if check and proc.returncode != 0:
        fail(f"`{printable}` exited {proc.returncode}")
    return proc.stdout


def stage_login() -> None:
    """Persist the CLI login without the token entering argv or the log.

    Same reasoning as module 08's stage_cli_login: `preloop login --token <t>`
    would expose the key in this job's process listing and in any `set -x`
    trace. The CLI reads exactly this file, so writing it is equivalent.
    """
    config_path = HOME / cli_onboard.CLI_CONFIG_RELPATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(cli_onboard.login_config_yaml(TOKEN, URL))
    config_path.chmod(0o600)


def plant_claude_code_install() -> None:
    """Minimal Claude Code install that discovery recognises, in API-key mode.

    `~/.claude.json` is what the discoverer keys on; ANTHROPIC_API_KEY makes
    the auth state 'ready' via an API key rather than a subscription token.
    """
    (HOME / ".claude").mkdir(parents=True, exist_ok=True)
    (HOME / ".claude.json").write_text("{}\n")
    os.environ["ANTHROPIC_API_KEY"] = PLACEHOLDER_UPSTREAM_KEY


def gateway_request(base_url: str, api_key: str, model: str) -> tuple[int, str]:
    """One Anthropic-shaped request through the Preloop gateway."""
    body = json.dumps(
        {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def account_gateway_requests() -> int:
    """Total metered gateway requests on the account.

    Deliberately the *summary* endpoint, not gateway-usage/search: the search
    corpus skips interactions with status >= 400 unless
    model_gateway_auto_index_failed_interactions is on (default off), so a
    request that the gateway accepted but upstream rejected is metered in
    ApiUsage yet absent from search. total_requests counts the ApiUsage rows,
    which is exactly the fact under test. include_breakdown=false keeps the
    response small.
    """
    status, body = riglib.api_request(
        f"{URL}/api/v1/account/gateway-usage/summary?include_breakdown=false",
        token=TOKEN,
    )
    if status != 200:
        fail(f"gateway-usage/summary failed ({status}): {riglib.redact(body)}")
    return int((body or {}).get("total_requests") or 0)


def main() -> None:
    if not URL or not TOKEN:
        fail("PRELOOP_TEST_URL and PRELOOP_TEST_API_KEY are required")

    print("== login (staged via the CLI's own config file) ==", flush=True)
    stage_login()
    status_output = cli("auth", "status")
    # The degraded variants ("Authenticated (stored login could not be
    # refreshed)", "Authenticated (token may be invalid ...)") must not pass:
    # they mean the CLI never reached the server.
    if not any(line.strip() == "Authenticated" for line in status_output.splitlines()):
        fail("auth status did not report a verified login")

    print("== plant a Claude Code install (API-key mode) ==", flush=True)
    plant_claude_code_install()

    print("== onboard ==", flush=True)
    # --skip-live-validate: the placeholder upstream key cannot answer a real
    # model prompt. Gateway wiring and metering are what this job proves;
    # module 08 exercises live validation against real agent credentials.
    cli("agents", "onboard", "Claude Code", "--yes", "--skip-live-validate")

    print("== assert the enrollment ==", flush=True)
    listing = cli("agents", "list", "--json")
    save_artifact("agents.json", listing)
    agents = cli_onboard.agents_from_list_json(listing)
    claude = cli_onboard.find_agents_by_source_type(agents, "claude_code")
    if not claude:
        fail(
            "no claude_code managed agent after onboarding; "
            f"saw: {cli_onboard.onboarded_agent_names(agents)}"
        )
    if not any(cli_onboard.is_gateway_routed(a) for a in claude):
        fail("claude_code agent enrolled but reports no model-gateway routing")

    print("== drive one request through the minted credential ==", flush=True)
    settings_path = HOME / ".claude" / "settings.json"
    if not settings_path.exists():
        fail(f"onboarding wrote no {settings_path}")
    settings = json.loads(settings_path.read_text())
    base_url, api_key = cli_onboard.gateway_env_from_settings(settings)
    if not base_url or not api_key:
        fail("onboarding wrote no ANTHROPIC_BASE_URL/ANTHROPIC_API_KEY")
    if api_key == PLACEHOLDER_UPSTREAM_KEY:
        fail("onboarding left the upstream key in place; no credential was minted")
    model = str(settings.get("model") or "claude-sonnet-4-5")

    before = account_gateway_requests()
    print(f"gateway requests before: {before}", flush=True)

    code, body = gateway_request(base_url, api_key, model)
    save_artifact("gateway-response.json", body)
    print(f"gateway responded {code}: {riglib.redact(body)}", flush=True)
    if code in (401, 403):
        # Preloop itself refused the credential it just minted — precisely the
        # halt/re-onboard class of bug this job exists to catch.
        fail("the Preloop gateway rejected the credential onboarding minted")

    print("== assert the request was metered ==", flush=True)
    # Usage is written on the request path, but the response can return before
    # the commit is visible to a follow-up read; poll briefly.
    total = before
    for attempt in range(1, 7):
        total = account_gateway_requests()
        print(f"gateway requests now: {total} (attempt {attempt})", flush=True)
        if total > before:
            break
        time.sleep(5)
    if total <= before:
        fail(f"no usage row recorded (before={before}, after={total})")

    print("CLI onboarding e2e passed", flush=True)


if __name__ == "__main__":
    main()

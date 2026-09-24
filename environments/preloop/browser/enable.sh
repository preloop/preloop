#!/usr/bin/env bash
# Render the Playwright MCP config, register it with the harness, then prove
# the browser cannot bypass the egress proxy. A failed self-check aborts setup.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${ROOT}/playwright-mcp.config.json"
# Pin recorded for the sandbox image. The matching Playwright version is
# pinned in environments/preloop/tools/package.json.
PLAYWRIGHT_MCP_VERSION="0.0.82"

if [[ -z "${PRELOOP_BROWSER_PROXY:-}" ]]; then
  exec bash "${ROOT}/selfcheck.sh"
fi

case "${PRELOOP_HARNESS:-}" in
  codex | claude) ;;
  *)
    printf '%s\n' "browser_harness_unsupported"
    exit 78
    ;;
esac

# Only a simple http(s) origin is substituted into JSON and TOML.
if [[ ! "${PRELOOP_BROWSER_PROXY}" =~ ^https?://[A-Za-z0-9._:-]+$ ]]; then
  printf '%s\n' "browser_egress_not_enforced"
  exit 1
fi

rest="${PRELOOP_BROWSER_PROXY#*://}"
hostport="${rest%%/*}"
hostport="${hostport#*@}"
if [[ "${hostport}" == \[* ]]; then
  PRELOOP_BROWSER_PROXY_HOST="${hostport#\[}"
  PRELOOP_BROWSER_PROXY_HOST="${PRELOOP_BROWSER_PROXY_HOST%%]*}"
else
  PRELOOP_BROWSER_PROXY_HOST="${hostport%%:*}"
fi
if [[ -z "${PRELOOP_BROWSER_PROXY_HOST}" || ! "${PRELOOP_BROWSER_PROXY_HOST}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf '%s\n' "browser_egress_not_enforced"
  exit 1
fi
export PRELOOP_BROWSER_PROXY_HOST

STATE_DIR="${PRELOOP_BROWSER_STATE_DIR:-/tmp/preloop-browser}"
mkdir -p "${STATE_DIR}"
PRELOOP_BROWSER_CONFIG="${STATE_DIR}/playwright-mcp.config.json"
export PRELOOP_BROWSER_CONFIG

python3 - "${TEMPLATE}" "${PRELOOP_BROWSER_CONFIG}" "${PRELOOP_BROWSER_PROXY}" "${PRELOOP_BROWSER_PROXY_HOST}" <<'PY'
import json
import sys

template, dest, proxy, host = sys.argv[1:5]
text = open(template, encoding="utf-8").read()
# The host placeholder contains the proxy placeholder as a prefix.
text = text.replace("${PRELOOP_BROWSER_PROXY_HOST}", host)
text = text.replace("${PRELOOP_BROWSER_PROXY}", proxy)
if "${" in text:
    sys.exit("unreplaced placeholder")
parsed = json.loads(text)
args = parsed["browser"]["launchOptions"]["args"]
expected = {
    f"--proxy-server={proxy}",
    f"--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE {host}",
    "--proxy-bypass-list=<-loopback>",
}
if set(args) != expected or parsed["browser"].get("isolated") is not True:
    sys.exit("rendered config missing chromium flags")
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text)
    if not text.endswith("\n"):
        handle.write("\n")
PY

export PLAYWRIGHT_MCP_VERSION
python3 - "${PRELOOP_HARNESS}" <<'PY'
import json
import os
import pathlib
import sys

harness = sys.argv[1]
version = os.environ["PLAYWRIGHT_MCP_VERSION"]
config = os.environ["PRELOOP_BROWSER_CONFIG"]
proxy = os.environ["PRELOOP_BROWSER_PROXY"]
binary = os.environ.get(
    "PRELOOP_PLAYWRIGHT_MCP_BIN",
    "/opt/preloop-env-tools/node_modules/.bin/playwright-mcp",
)
args = [
    "--config",
    config,
    "--proxy-server",
    proxy,
    "--isolated",
    "--headless",
]

def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

if harness == "codex":
    home = pathlib.Path(os.environ.get("HOME", "/tmp"))
    codex = home / ".codex"
    codex.mkdir(parents=True, exist_ok=True)
    fragment = (
        f"# @playwright/mcp@{version}\n"
        "[mcp_servers.browser]\n"
        f"command = {toml_string(binary)}\n"
        "args = [" + ", ".join(toml_string(item) for item in args) + "]\n"
    )
    (codex / "preloop-browser-mcp.toml").write_text(fragment, encoding="utf-8")
    config_path = codex / "config.toml"
    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    kept: list[str] = []
    skipping = False
    for line in existing.splitlines(keepends=True):
        if line.startswith("[mcp_servers.browser]"):
            skipping = True
            continue
        if skipping and line.startswith("["):
            skipping = False
        if not skipping:
            kept.append(line)
    body = "".join(kept).rstrip()
    if body:
        body += "\n\n"
    config_path.write_text(body + fragment, encoding="utf-8")
else:
    path = pathlib.Path(os.environ.get("PRELOOP_MCP_JSON", ".mcp.json"))
    data: dict = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["browser"] = {"command": binary, "args": args}
    data["mcpServers"] = servers
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

exec bash "${ROOT}/selfcheck.sh"

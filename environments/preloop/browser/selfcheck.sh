#!/usr/bin/env bash
# Prove Chromium's only network path is the egress proxy. Anything other than
# a proxy denial of both probes fails closed with browser_egress_not_enforced.
set -euo pipefail

fail() {
  printf '%s\n' "browser_egress_not_enforced"
  exit 1
}

if [[ -z "${PRELOOP_BROWSER_PROXY:-}" ]]; then
  fail
fi

if [[ ! "${PRELOOP_BROWSER_PROXY}" =~ ^https?://[A-Za-z0-9._:-]+$ ]]; then
  fail
fi

python3 - "${PRELOOP_BROWSER_PROXY}" <<'PY' || fail
import sys
import urllib.request

proxy = sys.argv[1].rstrip("/")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(proxy + "/healthz", timeout=5) as response:
        body = response.read().decode("utf-8", "replace").strip()
        if response.status != 200 or body != "ok":
            sys.exit(1)
except Exception:
    sys.exit(1)
PY

if [[ -z "${PRELOOP_BROWSER_CONFIG:-}" || ! -f "${PRELOOP_BROWSER_CONFIG}" ]]; then
  fail
fi

NODE_PATH="${PRELOOP_PLAYWRIGHT_NODE_PATH:-/opt/preloop-env-tools/node_modules}${NODE_PATH:+:${NODE_PATH}}"
export NODE_PATH

if ! command -v node >/dev/null 2>&1; then
  fail
fi

node --input-type=commonjs - "${PRELOOP_BROWSER_CONFIG}" <<'JS' || fail
const fs = require("fs");
const configPath = process.argv[2];
let playwright;
try {
  playwright = require("playwright");
} catch (err) {
  console.error(String(err));
  process.exit(1);
}

const PROXY_ERROR =
  /ERR_TUNNEL_CONNECTION_FAILED|ERR_PROXY_CONNECTION_FAILED|ERR_PROXY_CERTIFICATE_INVALID|ERR_PROXY_AUTH_UNSUPPORTED|ERR_PROXY_AUTH_REQUESTED|egress_denied/;
// The required host-resolver-rules map every name, including the metadata
// literal, to ~NOTFOUND before a dial. That failure is enforcement of the
// resolver flag. A hostname probe must still be a proxy or tunnel error so a
// broken resolver cannot impersonate an allowlist denial.
const RESOLVER_BLOCK =
  /ERR_NAME_NOT_RESOLVED|chrome-error:\/\/chromewebdata|interrupted by another navigation/;

async function main() {
  const parsed = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const args = (parsed.browser && parsed.browser.launchOptions && parsed.browser.launchOptions.args) || [];
  const proxyFlag = args.some((item) => String(item).startsWith("--proxy-server="));
  const resolverFlag = args.some((item) =>
    String(item).startsWith("--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE ")
  );
  if (!proxyFlag || !resolverFlag) {
    process.exit(1);
  }
  const launchArgs = args.slice();
  if (typeof process.getuid === "function" && process.getuid() === 0) {
    launchArgs.push("--no-sandbox");
  }
  const browser = await playwright.chromium.launch({
    headless: true,
    executablePath: playwright.chromium.executablePath(),
    args: launchArgs,
  });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const urls = ["http://169.254.169.254/", "https://example.org/"];
    for (const url of urls) {
      const blocked = await probe(page, url);
      if (!blocked) {
        console.error("probe_not_blocked " + url);
        process.exit(1);
      }
      console.log("browser_probe_blocked " + url);
    }
  } finally {
    await browser.close();
  }
}

async function probe(page, url) {
  try {
    const response = await page.goto(url, { timeout: 15000, waitUntil: "commit" });
    const status = response ? response.status() : 0;
    let body = "";
    try {
      body = response ? await response.text() : "";
    } catch (err) {
      body = String(err);
    }
    return classify(url, status + "\n" + body);
  } catch (err) {
    return classify(url, String(err));
  }
}

function classify(url, detail) {
  if (PROXY_ERROR.test(detail)) {
    return true;
  }
  return url.startsWith("http://169.254.169.254") && RESOLVER_BLOCK.test(detail);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
JS

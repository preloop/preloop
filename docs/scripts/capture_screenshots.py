"""Capture the documentation and landing screenshots from a local stack.

Dark theme, 1920x1080 viewport, device scale factor 2, PNG, no annotations.
Run it against the local screenshot stack described in README.md, never
against staging or production. File names are stable: every capture
overwrites the file the docs or the landing page already reference.

    python docs/scripts/capture_screenshots.py            # everything
    python docs/scripts/capture_screenshots.py --only dashboard,cost_page

The flow captures create a "Contract Payment Processor" flow through the
console form and start one run that stops at the Support approval rule, so run
them once per fresh stack.
"""

import argparse
import asyncio
import time
from pathlib import Path

import httpx
from PIL import Image
from playwright.async_api import Page, async_playwright

REPO = Path(__file__).resolve().parents[2]
DOCS_SHOTS = REPO / "docs" / "assets" / "screenshots"
LANDING_DARK = (
    REPO / "frontend" / "public" / "assets" / "screenshots" / "quickstart" / "dark"
)
VIEWPORT = {"width": 1920, "height": 1080}
SCALE = 2
# Stills the landing page serves with -800/-1600 webp derivatives.
# audit_page is one of them but is not captured here: the audit timeline
# reads /api/v1/audit-logs, which the open-source backend does not serve.
LANDING_WEBP = {"agent_bubble", "cost_page", "dashboard", "rules_configured"}

FLOW_NAME = "Contract Payment Processor"
# Name, description and prompt match docs/guide/quickstart-flows.md, Step 2.
FLOW_DESCRIPTION = "Process contract payments with approval for large amounts"
FLOW_PROMPT = """You are a payment processor. Process the payment with these details:

Recipient: {{trigger_event.payload.recipient}}
Amount: ${{trigger_event.payload.amount}}
Contract ID: {{trigger_event.payload.contract_id}}

Use the pay tool to send the payment. The tool is configured with
an approval workflow - small amounts are auto-approved, larger amounts
require human approval.

After payment completes, report the status. Do not retry if declined."""
# Step 3 values: above the $100 auto-allow rule, so the Support workflow approves.
TEST_VALUES = {
    "trigger_event.payload.recipient": "contractor@example.com",
    "trigger_event.payload.amount": "150",
    "trigger_event.payload.contract_id": "CONTRACT-2026-001",
}

DEEP_QUERY = """
const deepQuery = (root, selector) => {
  const direct = root.querySelector?.(selector);
  if (direct) return direct;
  for (const el of root.querySelectorAll?.('*') || []) {
    if (el.shadowRoot) {
      const found = deepQuery(el.shadowRoot, selector);
      if (found) return found;
    }
  }
  return null;
};
"""


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--base-url",
        default="http://127.0.0.1:18373",
        help="console URL of the local stack",
    )
    p.add_argument("--api-url", default="http://127.0.0.1:18300")
    p.add_argument("--gateway-url", default="http://127.0.0.1:18301")
    p.add_argument("--username", default="alex")
    p.add_argument("--password", default="local-test-pass-2026")
    p.add_argument("--only", default="", help="comma separated capture names")
    p.add_argument("--headed", action="store_true")
    return p.parse_args()


def save_png(src: bytes, *targets: Path) -> None:
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src)
        print("saved", target.relative_to(REPO))


def write_webp(png: Path) -> None:
    img = Image.open(png).convert("RGB")
    for width in (800, 1600):
        height = round(img.height * width / img.width)
        out = png.with_name("%s-%d.webp" % (png.stem, width))
        img.resize((width, height), Image.LANCZOS).save(
            out, "WEBP", quality=82, method=6
        )
        print("saved", out.relative_to(REPO))


async def shoot(page: Page, name: str, *targets: Path) -> None:
    await page.mouse.move(0, VIEWPORT["height"] - 1)
    await page.wait_for_timeout(600)
    data = await page.screenshot(type="png", full_page=False)
    save_png(data, *targets)
    for t in targets:
        if t.parent == LANDING_DARK and t.stem in LANDING_WEBP:
            write_webp(t)


async def wait_view(page: Page, tag: str, timeout: int = 30_000) -> None:
    await page.wait_for_function(
        "(sel) => {"
        + DEEP_QUERY
        + "const v = deepQuery(document, sel); return v && !v.loading; }",
        arg=tag,
        timeout=timeout,
    )
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)


async def login(page: Page, args) -> None:
    await page.goto(args.base_url + "/login")
    await page.locator('sl-input[name="username"] input').fill(args.username)
    await page.locator('sl-input[name="password"] input').fill(args.password)
    await page.locator('sl-button[type="submit"]').click()
    await page.wait_for_url("**/console**", timeout=60_000)
    await page.wait_for_load_state("networkidle")


def api_client(args) -> httpx.Client:
    r = httpx.post(
        args.api_url + "/api/v1/auth/token",
        data={"username": args.username, "password": args.password},
        timeout=30,
    )
    r.raise_for_status()
    return httpx.Client(
        base_url=args.api_url,
        timeout=60,
        headers={"Authorization": "Bearer " + r.json()["access_token"]},
    )


# Captures ---------------------------------------------------------------


async def cap_dashboard(page, args, api):
    await page.goto(args.base_url + "/console")
    await wait_view(page, "dashboard-view")
    await shoot(
        page,
        "dashboard",
        DOCS_SHOTS / "quickstart/dark/dashboard.png",
        LANDING_DARK / "dashboard.png",
    )


async def cap_cost_page(page, args, api):
    await page.goto(args.base_url + "/console/cost")
    await wait_view(page, "cost-view")
    await shoot(
        page,
        "cost_page",
        DOCS_SHOTS / "quickstart/dark/cost_page.png",
        LANDING_DARK / "cost_page.png",
    )


async def cap_rules_configured(page, args, api):
    await page.goto(args.base_url + "/console/tools")
    await wait_view(page, "tools-view")
    pay = page.locator('tool-list-item:has-text("pay")').first
    await pay.wait_for(state="visible", timeout=20_000)
    if not await pay.evaluate("(el) => Boolean(el.expanded)"):
        await pay.locator(".tool-header").click()
        await page.wait_for_timeout(800)
    await pay.evaluate("(el) => el.scrollIntoView({block: 'nearest'})")
    await page.wait_for_timeout(800)
    await shoot(page, "rules_configured", LANDING_DARK / "rules_configured.png")


async def cap_agent_bubble(page, args, api):
    """Agents canvas with a live bubble from one real gateway call."""
    await page.goto(args.base_url + "/console/agents")
    await wait_view(page, "agents-view")
    await page.get_by_role("button", name="Canvas").click()
    await page.wait_for_function(
        "() => {"
        + DEEP_QUERY
        + "const r = deepQuery(document, 'agents-view')?.shadowRoot;"
        " return r && r.querySelector('.gateway-node') && r.querySelectorAll('.agent-node').length > 0; }",
        timeout=30_000,
    )
    await page.wait_for_timeout(2000)
    agents = api.get("/api/v1/agents").json().get("items") or []
    agent = next(a for a in agents if a["display_name"].startswith("Claude Code"))
    cred = api.post(
        "/api/v1/agents/%s/credentials" % agent["id"],
        json={"name": "screenshot bubble %d" % int(time.time())},
    ).json()
    token = (
        cred.get("token")
        or cred.get("secret")
        or cred.get("key")
        or (cred.get("credential") or {}).get("token")
    )
    httpx.post(
        args.gateway_url + "/openai/v1/chat/completions",
        timeout=60,
        headers={"Authorization": "Bearer " + token},
        json={
            "model": "openai/gpt-5.4",
            "messages": [
                {
                    "role": "user",
                    "content": "Refactor the billing module and add tests.",
                }
            ],
        },
    ).raise_for_status()
    await page.wait_for_function(
        "() => {"
        + DEEP_QUERY
        + "const r = deepQuery(document, 'agents-view')?.shadowRoot;"
        " return r && r.querySelector('[class*=bubble]'); }",
        timeout=20_000,
    )
    await page.wait_for_timeout(700)
    await shoot(page, "agent_bubble", LANDING_DARK / "agent_bubble.png")


async def cap_optimize_tab(page, args, api):
    sessions = api.get("/api/v1/runtime-sessions", params={"limit": 50}).json()
    items = (
        sessions
        if isinstance(sessions, list)
        else (sessions.get("items") or sessions.get("sessions") or [])
    )
    best = max(items, key=lambda s: float(s.get("estimated_cost") or 0))
    await page.goto(
        args.base_url
        + "/console/runtime-sessions?sessionId=%s&replay=optimize" % best["id"]
    )
    await page.wait_for_load_state("networkidle")
    gen = page.get_by_role("button", name="Generate suggestions")
    await gen.wait_for(state="visible", timeout=30_000)
    await gen.click()
    await page.get_by_text("Potential savings").wait_for(timeout=90_000)
    await page.wait_for_timeout(1000)
    await page.get_by_text("Optimization Suggestions").evaluate(
        "(el) => el.scrollIntoView({block: 'start'})"
    )
    await page.wait_for_timeout(800)
    await shoot(page, "optimize-tab", DOCS_SHOTS / "sessions/dark/optimize-tab.png")


async def open_blank_flow(page, args):
    await page.goto(args.base_url + "/console/flows/new")
    await page.get_by_text("Blank flow", exact=True).click()
    await page.locator('sl-input[label="Flow name"]').wait_for(timeout=20_000)
    await page.wait_for_timeout(1000)


async def cap_add_ai_model_dialog(page, args, api):
    await open_blank_flow(page, args)
    await page.locator('sl-button:has-text("Add AI")').first.click()
    dialog = page.locator("add-ai-model-modal sl-dialog[open]")
    await dialog.wait_for(timeout=20_000)
    await dialog.locator('sl-input[label="Name"] input').fill("GPT-5.4")
    await dialog.locator('sl-select[label="Provider"]').click()
    await (
        dialog.locator('sl-select[label="Provider"] sl-option')
        .filter(has_text="OpenAI")
        .first.click()
    )
    await page.wait_for_timeout(1500)
    models = dialog.locator('sl-select[label="Model Name / ID"]')
    if await models.count():
        await models.click()
        await dialog.locator(
            'sl-select[label="Model Name / ID"] sl-option[value="gpt-5.4"]'
        ).click()
    key = dialog.locator('sl-input[label="API key"] input')
    if await key.count():
        await key.first.fill("sk-local-stub-not-a-real-key")
    await page.wait_for_timeout(800)
    await shoot(
        page, "add-ai-model-dialog", DOCS_SHOTS / "quickstart/add-ai-model-dialog.png"
    )
    await page.keyboard.press("Escape")


async def cap_flow_create_form(page, args, api):
    await open_blank_flow(page, args)
    await page.locator('sl-input[label="Flow name"] input').fill(FLOW_NAME)
    await page.locator('sl-textarea[label="Description"] textarea').fill(
        FLOW_DESCRIPTION
    )
    runtime = page.locator('sl-select[label="Agent runtime"]')
    await runtime.click()
    await page.locator('sl-option:has-text("OpenCode")').first.click()
    model = page.locator('sl-select[label="AI model"]')
    await model.click()
    await (
        page.locator('sl-select[label="AI model"] sl-option')
        .filter(has_text="GPT-5.4")
        .first.click()
    )
    await page.locator('sl-textarea[label="Prompt template"] textarea').fill(
        FLOW_PROMPT
    )
    pay = (
        page.locator('sl-checkbox:has-text("pay")')
        .filter(has_text="Example MCP Server")
        .first
    )
    if not await pay.evaluate("(el) => el.checked"):
        await pay.click()
    await pay.evaluate("(el) => el.scrollIntoView({block: 'center'})")
    await page.wait_for_timeout(800)
    await shoot(
        page, "flow-create-form", DOCS_SHOTS / "quickstart/flow-create-form.png"
    )
    await page.locator('sl-button:has-text("Create flow")').last.click()
    await page.wait_for_url("**/console/flows/*-*", timeout=30_000)
    await page.wait_for_load_state("networkidle")


async def cap_flow_run(page, args, api):
    flows = api.get("/api/v1/flows").json()
    flows = flows if isinstance(flows, list) else flows.get("items", [])
    flow = next(f for f in flows if f["name"] == FLOW_NAME)
    await page.goto(args.base_url + "/console/flows/" + flow["id"])
    await wait_view(page, "flow-view")
    await page.locator('sl-button:has-text("Run now") >> visible=true').first.click()
    dialog = page.locator('sl-dialog[label="Values for the trigger event"]')
    await dialog.locator("sl-input").first.wait_for(timeout=20_000)
    for label, value in TEST_VALUES.items():
        await dialog.locator('sl-input[label="%s"] input' % label).fill(value)
    await page.wait_for_timeout(800)
    await shoot(
        page, "flow-test-run-dialog", DOCS_SHOTS / "quickstart/flow-test-run-dialog.png"
    )
    await dialog.locator('sl-button:has-text("Run now")').click()
    await page.wait_for_url("**/console/flows/executions/*", timeout=30_000)
    await wait_view(page, "flow-execution-view")
    execution_id = page.url.rstrip("/").rsplit("/", 1)[-1]
    # Started: wait until the agent's container is running.
    for _ in range(60):
        status = (
            api.get("/api/v1/flows/executions/" + execution_id).json().get("status")
        )
        if status and status.upper() == "RUNNING":
            break
        await page.wait_for_timeout(2000)
    await page.wait_for_timeout(4000)
    await shoot(
        page,
        "flow-execution-started",
        DOCS_SHOTS / "quickstart/flow-execution-started.png",
    )
    for _ in range(90):
        status = (
            api.get("/api/v1/flows/executions/" + execution_id).json().get("status")
        )
        if status and status.upper() == "WAITING_FOR_HUMAN":
            break
        if status and status.upper() in (
            "FAILED",
            "SUCCEEDED",
            "COMPLETED",
            "CANCELLED",
        ):
            raise SystemExit("execution ended as %s before the approval gate" % status)
        await page.wait_for_timeout(2000)
    else:
        raise SystemExit("execution never reached the approval gate")
    await page.reload()
    await wait_view(page, "flow-execution-view")
    await page.wait_for_timeout(3000)
    await shoot(
        page,
        "flow-execution-waiting-approval",
        DOCS_SHOTS / "quickstart/flow-execution-waiting-approval.png",
    )


# Order matters: the flow captures create state the earlier ones should not show.
CAPTURES = [
    ("dashboard", cap_dashboard),
    ("cost_page", cap_cost_page),
    ("rules_configured", cap_rules_configured),
    ("agent_bubble", cap_agent_bubble),
    ("optimize-tab", cap_optimize_tab),
    ("add-ai-model-dialog", cap_add_ai_model_dialog),
    ("flow-create-form", cap_flow_create_form),
    ("flow-run", cap_flow_run),
]


async def main():
    args = parse_args()
    only = {n.strip() for n in args.only.split(",") if n.strip()}
    api = api_client(args)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        # UTC: the API returns naive UTC timestamps, which the console reads
        # as local time; in UTC the relative times ("2m ago") come out right.
        context = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=SCALE,
            color_scheme="dark",
            timezone_id="UTC",
        )
        await context.add_init_script(
            "try { localStorage.setItem('theme', 'dark');"
            " localStorage.setItem('dashboard_welcome_dismissed', 'true'); } catch (e) {}"
        )
        page = await context.new_page()
        await login(page, args)
        for name, fn in CAPTURES:
            if only and name not in only:
                continue
            print("capturing", name)
            await fn(page, args, api)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

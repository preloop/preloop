# Documentation screenshots

`capture_screenshots.py` recaptures the console screenshots used by the docs
and the landing page. It runs against a local stack only, never staging or
production, and it uses no real provider keys: every model call goes to a
local OpenAI-compatible stub.

Output: dark theme, no annotations, framed like the images they replace so the
console content keeps the same size in the page:

| Images | CSS viewport | File size |
| --- | --- | --- |
| docs `*/dark/*.png` (dashboard, cost, optimize tab) | 1600x950 | 1600x950 |
| docs `quickstart/*.png` (add model, flow form, run dialog, executions) | 1400x900 | 1400x900 |
| landing `frontend/public/assets/screenshots/quickstart/dark/*.png` | 1600x950 | 3200x1900 |

Pages render at device scale factor 2; docs files are downscaled to 1x. A
wider viewport makes the content column smaller relative to the image, which
is why the framing is fixed. File names are stable, so pages need no edits
when you rerun it. For the landing stills it also writes the `-800.webp` and
`-1600.webp` derivatives the landing page serves.

## What is in `screenshot-stack/`

| File | Purpose |
| --- | --- |
| `compose.ee.yml` | Optional overlay for an EE backend image: drops the `.:/app` source mount so the baked-in EE plugins load. |
| `compose.screenshots.yml` | Compose overlay: remapped ports (console 18373, API 18300, gateway 18301, stub 18390), `PRELOOP_DISABLE_TELEMETRY=true` on every Preloop service, the model stub and the example MCP server. |
| `opencode.Dockerfile` | OpenCode agent image with a writable `/workspace`, so a local flow run can start. |
| `stub_model.py` | OpenAI-compatible stub. Returns fixed replies with token usage, calls `pay` for flow prompts, and answers approval-summary prompts. |
| `seed.py` | Signs up a local user and adds the example MCP server, the quickstart `pay` rules and workflows, two stub models, an API key, three agents, an account budget and two preset flows. Then 20 gateway sessions and 7 MCP calls through the tool firewall: one allowed, one denied, one approved and one declined through the approvals API while the agent waits, one left pending for Support. |

## Which backend

The Overview Activity panel, its Tool firewall counter and the landing
`audit_page` read `/api/v1/audit-logs`, which only the EE audit plugin serves.
On the open-source backend those panels stay empty however much traffic you
send. The published images were captured on an EE backend built from this
checkout: the preloop-ee Dockerfile with this repository as `preloop/`,
tagged `preloop-shots/preloop-ee:local`. To use it, export
`PRELOOP_SHOTS_IMAGE=preloop-shots/preloop-ee:local` and add
`-f docs/scripts/screenshot-stack/compose.ee.yml` after the screenshots
overlay. The seed account is on the EE free plan, so the seed creates three
agents (the plan's cap), skips budget notifications and does not add a second
user. The capture closes the "3 of 3 agents" plan banner with its own close
button.

## Run it

From the repository root (open-source backend shown; see above for EE):

```bash
docker build -t preloop-shots/preloop:local .
docker build -t preloop-shots/console:dev -f frontend/Dockerfile.dev frontend
docker build -t preloop-shots/opencode:local -f docs/scripts/screenshot-stack/opencode.Dockerfile docs/scripts/screenshot-stack

COMPOSE="docker compose -p preloopshots -f docker-compose.yml -f docker-compose.override.yml -f docs/scripts/screenshot-stack/compose.screenshots.yml"
$COMPOSE up -d

docker run --rm -e PRELOOP_DISABLE_TELEMETRY=true \
  --network preloopshots_default \
  -v "$PWD/docs/scripts/screenshot-stack:/kit:ro" \
  preloop-shots/preloop:local python /kit/seed.py

python -m pip install playwright pillow httpx
python -m playwright install chromium
python docs/scripts/capture_screenshots.py            # all captures
python docs/scripts/capture_screenshots.py --only dashboard --headed
python docs/scripts/capture_screenshots.py --only audit_page   # EE backend only

$COMPOSE down -v
```

Seed a fresh stack (`down -v` first) before a full run: the flow
captures create the "Contract Payment Processor" flow, and the numbers on the
dashboard and cost page come from the seed.

The browser runs with `timezone_id="UTC"`, because the API returns naive UTC
timestamps and pending approvals otherwise render as expired in other time
zones.

## Not captured here

- `settings/*.png` (users, teams, invitations): EE-only screens.
- `quickstart/mobile_approval.png`: a mobile app screenshot.
- Landing animations (`.mp4`, `agents-onboarding.webp`): recorded, not
  captured.

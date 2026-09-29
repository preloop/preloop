# Documentation screenshots

`capture_screenshots.py` recaptures the console screenshots used by the docs
and the landing page. It runs against a local stack only, never staging or
production, and it uses no real provider keys: every model call goes to a
local OpenAI-compatible stub.

Output: dark theme, 1920x1080 viewport, device scale factor 2 (3840x2160
PNG), no annotations. File names are stable, so pages need no edits when you
rerun it. For the four landing stills it also writes the `-800.webp` and
`-1600.webp` derivatives the landing page serves.

## What is in `screenshot-stack/`

| File | Purpose |
| --- | --- |
| `compose.screenshots.yml` | Compose overlay: remapped ports (console 18373, API 18300, gateway 18301, stub 18390), `PRELOOP_DISABLE_TELEMETRY=true` on every Preloop service, the model stub and the example MCP server. |
| `opencode.Dockerfile` | OpenCode agent image with a writable `/workspace`, so a local flow run can start. |
| `stub_model.py` | OpenAI-compatible stub. Returns fixed replies with token usage, calls `pay` for flow prompts, and answers approval-summary prompts. |
| `seed.py` | Signs up a local user, adds the example MCP server, the quickstart `pay` rules and workflows, two stub models, an API key, four agents, ten gateway sessions and a few MCP calls (one left pending approval). |

## Run it

From the repository root:

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

$COMPOSE down -v
```

Seed a fresh stack (`down -v` first) before a full run: the flow
captures create the "Contract Payment Processor" flow, and the numbers on the
dashboard and cost page come from the seed.

The browser runs with `timezone_id="UTC"`, because the API returns naive UTC
timestamps and pending approvals otherwise render as expired in other time
zones.

## Not captured here

- `audit_page` (landing): the audit timeline reads `/api/v1/audit-logs`,
  which the open-source backend does not serve. Capture it on an EE stack.
- `settings/*.png` (users, teams, invitations): EE-only screens.
- `quickstart/mobile_approval.png`: a mobile app screenshot.
- Landing animations (`.mp4`, `agents-onboarding.webp`): recorded, not
  captured.

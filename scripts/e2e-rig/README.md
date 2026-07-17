# Recorded lifecycle e2e rig (Phase 1 — OSS flow)

Scripted, fully-recorded end-to-end lifecycle of a Preloop OSS instance on a
real VM: offboard whatever is there, tear the instance down, reinstall a
pinned release behind TLS, sign up in a recorded browser, install the pinned
CLI, onboard every discoverable agent (recorded terminal), verify in the
console, run a custom agent through the gateway, (version-gated) optimize,
then offboard everything and assert each agent's model + MCP config was
restored byte-identically or semantically-equally.

The footage doubles as documentation/launch material: browser sessions are
recorded by Playwright (headed by default), terminal work is captured in a
real pty with pexpect into asciicast v2 and rendered with agg + ffmpeg
(never vhs — it stalls), and every run is stitched into one continuous
`<run-id>-oss-full-run.mp4` with per-step title cards.

The rig always sets `PRELOOP_DISABLE_TELEMETRY=true` — locally, on every ssh
command, in the CLI installer invocation, and in the installed instance's
`.env` — to keep test runs out of adoption data.

Relationship to the release smoke test: CI's release gate
(`scripts/release_smoke_test.sh`, run by `.github/workflows/release.yml`)
verifies that a tagged release installs and comes up healthy. This rig is
the deep, recorded complement — the full lifecycle including agent
onboarding/offboarding and the restore assertion — run manually against a
real VM before/after a release.

## Usage

```bash
# Full run (destructive on the target instance — it reinstalls it):
scripts/e2e-rig/run-oss.sh \
  --host root@<vm-ssh-host> \
  --url https://<name>.nip.io \
  --release 0.11.1 --cli-version 0.11.1

# Teardown only (archives .env/config first, keeps Let's Encrypt certs):
scripts/e2e-rig/teardown-oss.sh --host root@<vm-ssh-host>
```

Options for `run-oss.sh`:

| flag | meaning |
|------|---------|
| `--headless` | browser modules run headless (default: headed, auto-falls back) |
| `--run-dir DIR` | reuse/resume a run's artifact dir (same creds and state) |
| `--from NN` / `--until NN` | run a module range |
| `--only NN[,NN]` | run specific modules against an existing `--run-dir` |
| `--keep-going` | do not stop on the first failed module |
| `--purge-certs` | teardown also deletes Let's Encrypt material (rate-limit risk) |
| `--no-video` | skip the full-run video compositing (fast iterations) |

Module exit codes: `0` pass, `3` skip (recorded with a note), anything else
fails the run.

Unit tests for the pure-python pieces (snapshot diff semantics, output
redaction) live in `tests/` — run them with `pytest scripts/e2e-rig/tests`.

## Requirements

- macOS (or Linux) driver machine with: `agg`, `ffmpeg` (brew), and a Python
  with `pexpect`, `playwright` (+ chromium), `Pillow`; auto-detected from the
  repo `.venv`, override with `RIG_PYTHON=/path/to/python`.
- Non-interactive ssh (`BatchMode`) to the VM as root.
- VM: Debian-ish with docker + docker compose v2, `npm` (for codex install),
  ports 80/443 free for the instance, DNS name resolving to it (nip.io is
  fine — but see CAA caveats in scripts/install-oss.sh).

## Modules

| # | what | artifacts |
|---|------|-----------|
| 01 | preflight + before-image snapshot of every agent's model/MCP config | `snapshots/before/` |
| 02 | offboard all enrolled agents with the CLI on the box, snapshot `baseline` | `logs/02-*`, `snapshots/baseline/` |
| 03 | archive `.env`/compose config, compose down `--volumes`, prune old images (certs kept) | `instance-archive/` |
| 04 | ensure claude/opencode (gate) + codex (best-effort, never credentials); refresh `baseline` | `logs/04-*` |
| 05 | install the pinned OSS release via the release's own `install-oss.sh`, TLS up | `logs/05-*` |
| 06 | poll `https://…/api/v1/health` with TLS verification ON, console + gateway reachable | `state/health.json` |
| 07 | recorded browser first-user signup, session + API token kept | `browser-videos/`, `screenshots/07-*` |
| 08 | recorded pty over ssh: pinned CLI install, staged login verified with `auth status`, `agents discover --yes` | `casts/08-*` |
| 09 | recorded browser: every onboarded agent visible + active on `/console/agents` | `screenshots/09-*` |
| 10 | register custom agent via API, mint gateway credential, run `research_agent.py` session through `/openai/v1` (recorded locally) | `casts/10-*`, `state/custom-agent.json` |
| 11 | optimize scene — probes the endpoint; SKIPs with a note on 0.11.x (feature lands in OSS 0.12.0/T2) | `screenshots/11-*` when present |
| 12 | recorded offboard of everything + custom-agent server cleanup + per-agent snapshot diff verdict | `casts/12-*`, `diffs/report.json` |

Then `run-summary.json` (steps, status, timings, notes) and
`<run-id>-oss-full-run.mp4` (title cards + all footage, chronological).

## The offboarding assertion

- `snapshots/before/` — untouched state at run start (informational diff).
- `snapshots/baseline/` — post-offboard, post-agent-install, pre-onboarding.
  This is what module 12 must restore to.
- `snapshots/final/` — after the final offboard.

Per config file: byte-identical wins; otherwise parsed-config comparison
with a small volatility allowlist (`~/.claude.json` is compared on its
managed surface only — `mcpServers` + `model` — because Claude Code churns
the rest on its own). Verdict is per agent over all its files; failures dump
unified diffs into `diffs/`.

## Secrets and safety

- Artifacts can contain agent config material (and archived instance `.env`
  holds secrets); the whole `artifacts/` dir is git-ignored — never commit it.
- The rig never touches agent auth/credential state on the VM (`~/.claude`
  auth, opencode auth, API keys). Snapshots are read-only; the only writes to
  agent configs are done by `preloop agents onboard/offboard` themselves.
- The CLI login is staged out-of-band: the token travels only over the ssh
  stdin stream into `install -m 600 /dev/stdin ~/.preloop/config.yaml` (the
  CLI's own login persistence target), so it never appears in any command
  line, environment, recording, or `ps` listing on either machine. The
  recorded scene verifies the login with `preloop auth status`.
- Failure output from API helpers is redacted (bearer/JWT/token-like strings
  and sensitive JSON values masked, bodies truncated) — see `redact()` in
  `lib/riglib.py` and `research_agent.py`.
- Let's Encrypt certs are preserved across teardowns by default (duplicate
  issuance is rate-limited to ~5/week).

## Known product findings baked into the rig

The three installer findings below are fixed by PR #65 (approved, not yet in
a release). Each corresponding workaround in the rig is marked with a
`# TODO(remove-after-#65)` comment — grep for it once a release ships with
the fix.

- installer: unattended TLS install dies under dash when no tty exists
  (`has_tty`'s `/dev/tty` redirect is fatal for a special builtin) — the rig
  runs the installer with bash.
- installer: re-install with an existing cert leaves `tls/active.conf`
  HTTP-only (`issue_certificate` early-returns before the https swap) — the
  rig applies a fixup and notes it.
- installer/teardown interplay: a preserved `.env` keeps
  `REGISTRATION_ENABLED=false` while the DB is gone — the rig deletes the
  instance config at teardown so a fresh install opens signup again.
- console: `/console/agents` cannot display `claude_desktop` agents (kind
  missing from the view's filter list) — recorded as a product-gap note.

## Cloud flow — TODO (Phase 1b)

The Cloud/Stripe flow (staging, Stripe TEST mode: fresh signup → onboarding →
first optimization free → second triggers the pay gate → 4242 checkout →
entitlement flips → proceed; teardown cancels the sub, deletes the customer,
cascade-deletes account+user) is NOT implemented yet — no Stripe test keys
were available. Reuse: modules 06-10 and the recording/compositing libs apply
as-is; needed additions are a staging-signup module, a pay-gate/checkout
browser module, and a Stripe/DB teardown module. The real-card production
check stays a manual founder run and is never automated.

## Phase 2 (post-launch, out of scope here)

Nightly CI, retries, full agent-matrix gating.

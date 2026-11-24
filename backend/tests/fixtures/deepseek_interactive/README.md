# Real-package interactive proof

The driver route was established on the published `@deepseek-ai/dsh@0.1.5-rc.2`
before integrating with Preloop. Install it in a disposable directory, then run:

```sh
PRELOOP_DISABLE_TELEMETRY=true node backend/tests/fixtures/deepseek_interactive/probe.mjs /path/to/node_modules/.bin/dsh
PRELOOP_DISABLE_TELEMETRY=true PROBE_PRODUCTION=1 node backend/tests/fixtures/deepseek_interactive/probe.mjs /path/to/node_modules/.bin/dsh
```

The first command exercises the minimal core driver; the second exercises the
production Cordis driver. Both assert three turns in one process, a different
process adopting the same persisted session, and first-turn context in the actual
resumed upstream request. All model responses are synthetic. Native title requests
are distinguished from conversation requests, and native runtime-context messages
are distinguished from human turns. No prompt-history replay is implemented.

## Route decision

The published `0.2.2-rc.5` headless README and executable help add `--session-id`
and `--json` but still specify one task per invocation, with no live follow-up.
Its upgrade guides also require Node 24.9+, change policy composition for custom
startup providers, change credential precedence and change filesystem/search
defaults. A pin bump therefore does not remove the need for a persistent driver.
The working `agent.followup()` / `agents.resume()` APIs on `0.1.5-rc.2` are used
instead. Both versions passed the existing real-package model-routing and native
approval-denial smoke. The newer version also accepted the reviewer preset's
prompt in that synthetic smoke, and the preset contract tests passed. This does
not claim a real-provider review or validate all of the newer release's behavior.
No pin bump is proposed.

## Local gateway and container proof

Use a disposable, migrated PostgreSQL database. Install this checkout into its
venv, the local harness plugin's locked npm dependencies, and pinned `dsh` on PATH.
Run from the repository root (set `DATABASE_URL` to that disposable database):

```sh
PRELOOP_DISABLE_TELEMETRY=true npm ci --prefix runtime-plugins/harness-preloop
PRELOOP_DISABLE_TELEMETRY=true PGOPTIONS='-c timezone=UTC' \
  DEEPSEEK_CONVERSATION_SMOKE=1 PYTHONPATH="$PWD/backend" \
  .venv/bin/pytest backend/tests/services/test_deepseek_conversation_integration.py -s
```

To repeat in fresh UID 10000 containers, build the fixture image from the repo
root and add `DEEPSEEK_CONVERSATION_IMAGE=preloop-deepseek-interactive-test:local`:

```sh
PRELOOP_DISABLE_TELEMETRY=true docker build \
  -f backend/tests/fixtures/deepseek_interactive/Dockerfile \
  -t preloop-deepseek-interactive-test:local .
```

This integration uses real runtime capability authentication, gateway bearer
authentication, provider HTTP streaming and usage persistence. The upstream is
synthetic. It verifies owner attribution across two executions, three live turns,
idle shutdown, native/workspace archive round-trips through encrypted artifact
storage, the same saved branch after the remote moves, uncommitted work and a
resumed fourth answer containing the first-turn context. The test coordinates
execution binding and archive capture; it does not run the background scheduler.
The Docker mode requires `host.docker.internal` to reach the local test gateway.

Observed on a local run with the fixture image already built (milliseconds to the
first native text delta at the supervisor): cold start **1923**, live follow-ups
**607** and **593**, resumed container **895**. Durable answer latencies were
**2047**, **655**, **645** and **944** respectively. These include container start
and runtime polling, but exclude image download/build and package installation.
They are synthetic-upstream measurements, not provider performance claims.

The ordinary unit tests cover failed native turns, child capability filtering,
archive identity/version/integrity, unrelated-file exclusion and launcher ordering.
The existing real-package plugin smoke covers model routing and native approval
denial; OpenCode driver tests are unchanged.

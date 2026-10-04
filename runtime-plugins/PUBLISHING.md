# Publishing Runtime Plugins

This guide covers publishing the standalone Preloop runtime plugins without
requiring the Preloop CLI on the target machine. The CLI can still provision
Agent Control config, but marketplace installation and runtime verification
must work from the agent runtime alone.

## Preferred Path: Manual GitLab CI Jobs

The enterprise repo pipeline (preloop-ee `.gitlab-ci.yml`) has manual publish
jobs mirroring `publish:langchain-preloop`:

- `publish:openclaw-plugin` builds and publishes `@preloop-ai/openclaw-plugin`
  to **both npm and ClawHub** in a single job (requires the `OPENCLAW_NPM_TOKEN`
  and `CLAWHUB_TOKEN` CI variables). The job then verifies that npm's
  `dist.shasum` matches ClawHub's `npmShasum` and fails if they differ.
- `publish:hermes-plugin` builds and publishes `preloop-hermes-plugin` to
  PyPI (requires the `HERMES_PYPI_TOKEN` CI variable).
- `publish:codex-plugin` builds, tests, and publishes `@preloop-ai/codex-plugin`
  to npm (requires the shared `NPM_TOKEN` CI variable). The job runs
  `npm ci`, `npm run build`, and `npm test` before publish, and refuses a
  version that already exists on npm.
- `publish:nanobot-plugin` builds and publishes `preloop-nanobot-plugin` to
  PyPI (requires the `NANOBOT_PYPI_TOKEN` CI variable). PyPI project-scoped
  tokens cannot exist before the first release, so the first publish needs
  an account-scoped token that is re-scoped to the project afterwards.
  Nanobot tests run in `test:unit:runtime-plugins`, which this job needs.

These jobs fail fast if a package version and its plugin manifest version
disagree (where a second manifest exists), or if the version is already on
the registry. Release flow:

1. Bump the versions (see Release Preconditions below) in the preloop repo.
2. Land the preloop submodule bump on preloop-ee `main`.
3. On that pipeline, trigger the manual publish job(s) from the `deploy` stage.
   For OpenClaw this publishes npm **and** ClawHub, with no follow-up step.

Plugin versions are decoupled from platform releases (a platform `0.11.x` tag
does not publish plugins); trigger these jobs whenever plugin changes land.

The sections below remain valid as the local/manual fallback and document the
ClawHub steps and smoke tests.

## Release Preconditions

- Bump matching versions in:
  - `openclaw-preloop/package.json`
  - `openclaw-preloop/openclaw.plugin.json`
  - `hermes-preloop/pyproject.toml`
  - `hermes-preloop/preloop-plugin.json`
  - `opencode-preloop/package.json`
  - `codex-preloop/package.json`
  - `nanobot-preloop/pyproject.toml`
  - `nanobot-preloop/preloop-plugin.json`
- Confirm the package names are final:
  - npm/OpenClaw: `@preloop-ai/openclaw-plugin`
  - PyPI/Hermes: `preloop-hermes-plugin`
  - npm/OpenCode: `@preloop-ai/opencode-plugin`
  - npm/Codex: `@preloop-ai/codex-plugin`
  - PyPI/Nanobot: `preloop-nanobot-plugin`
- Confirm Hermes entry points use the `hermes_agent.plugins` group and point at
  the module that exposes `register(ctx)` (`preloop_hermes_plugin.plugin`).
- Confirm OpenClaw `package.json` includes ClawHub-required metadata:
  `openclaw.compat.pluginApi` and `openclaw.build.openclawVersion`.
- Confirm each README includes CLI-free manual testing instructions.
- Run the runtime plugin tests:

```bash
cd preloop
pytest runtime-plugins/tests
```

## OpenClaw Plugin

Build and validate the npm package:

```bash
cd preloop/runtime-plugins/openclaw-preloop
npm ci
npm run build
npm pack --dry-run
npm publish --access public --dry-run
```

Publish to npm:

```bash
npm publish --access public
```

**ClawHub publication is automated in CI**, in the preloop-ee GitLab job
`publish:openclaw-plugin` and in the GitHub Actions workflow
`.github/workflows/publish-runtime-plugins.yml`. Both publish npm first, then
ClawHub from the same build, then assert the two registries serve the same
tarball. The steps below are the local fallback only.

### Why this is automated (and why it must stay that way)

In July 2026 npm and ClawHub diverged under the **same version 0.1.1**: ClawHub
served a Jul 10 build while npm served a Jul 18 build, differing by preloop
commit `6fb1bb79` ("Harden auth, policy evaluation, and approval startup
repair"). ClawHub was therefore serving pre-hardening code including an
already-fixed auth bypass. Any change here must preserve the digest guard.

### CI credentials

`CLAWHUB_TOKEN` is a ClawHub API token with publish rights on the `preloop-ai`
publisher. Generate it once on your workstation:

```bash
clawhub login    # device flow, opens a browser; one time only
clawhub token    # prints the stored API token
```

Store it as:

- GitLab (preloop-ee → Settings → CI/CD → Variables): **masked + protected**,
  named `CLAWHUB_TOKEN`.
- GitHub (preloop/preloop → Settings → Secrets → Environments → `release`):
  secret named `CLAWHUB_TOKEN`.

The old claim that ClawHub could not be automated "because `clawhub login` is
interactive" was wrong. The CLI ships `clawhub login --token <token>`, a global
`--no-input` flag, and per-command `--json` output specifically for CI.

### Local fallback

Install the CLI if needed (`npm install -g clawhub`), then:

```bash
# Provenance must point at the PUBLIC GitHub repo. `origin` is the internal
# GitLab (gitlab.spacecode.ai); `github` is github.com/preloop/preloop. The
# preloop repo is mirrored commit-for-commit, so the SHA is the same object on
# both, but only commits actually pushed to `github` will resolve for ClawHub.
SOURCE_REPO=preloop/preloop
SOURCE_COMMIT=$(git -C ../.. rev-parse HEAD)  # from openclaw-preloop/
SOURCE_PATH=runtime-plugins/openclaw-preloop

# Verify the commit is really on public GitHub before recording it:
curl -sfo /dev/null "https://api.github.com/repos/preloop/preloop/commits/$SOURCE_COMMIT" \
  || { echo "commit not on public GitHub: push the 'github' remote first"; exit 1; }

clawhub login --token "$CLAWHUB_TOKEN" --no-input
# First-time only: scoped npm names require a matching ClawHub publisher
clawhub publisher create preloop-ai --display-name "Preloop"
clawhub package validate .
clawhub package publish . --family code-plugin \
  --source-repo "$SOURCE_REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --source-path "$SOURCE_PATH" \
  --dry-run
clawhub package publish . --family code-plugin \
  --source-repo "$SOURCE_REPO" \
  --source-commit "$SOURCE_COMMIT" \
  --source-path "$SOURCE_PATH"

# ALWAYS finish with the divergence check:
VERSION=$(node -p "require('./package.json').version")
npm view "@preloop-ai/openclaw-plugin@$VERSION" dist.shasum
clawhub package inspect @preloop-ai/openclaw-plugin --version "$VERSION" --json \
  | node -p "JSON.parse(require('fs').readFileSync(0,'utf8')).package.artifact.npmShasum"
# these two MUST be identical
```

`--source-repo` and `--source-commit` must be set together. Use the public
GitHub repo (`preloop/preloop`) and a commit SHA that exists there. Prefer
also setting `--source-path` to the monorepo subpath so ClawHub can locate the
plugin sources. The package scope (`@preloop-ai/...`) must match an existing
ClawHub publisher handle (`preloop-ai`).

Publish ClawHub **after** npm: ClawHub fetches the npm tarball, so installs of
a version npm has not published yet fail at package fetch (observed as
`ENOENT ... /tmp/openclaw-clawhub-package-*.zip`) even though ClawHub already
lists the version's metadata.

### Removing a bad ClawHub version

There is no delete control in the ClawHub web UI, but the CLI has one:

```bash
clawhub package delete @preloop-ai/openclaw-plugin --version <version>
```

`--version` permanently deletes a single version. It **cannot be restored or
republished**: the version string is burned. If the version being deleted is
the current `latest`, publish the replacement first. Without `--version` the
command soft-deletes the whole package (reversible via
`clawhub package undelete`), which is almost never what you want.

The ClawHub/marketplace entry should install the npm package and run:

```bash
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
```

Manual smoke test on a machine without the Preloop CLI:

```bash
# The plugin is listed on ClawHub, so either spec installs it:
openclaw plugins install clawhub:@preloop-ai/openclaw-plugin
openclaw plugins install @preloop-ai/openclaw-plugin
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
preloop-openclaw-plugin run --config ~/.openclaw/openclaw.json
```

Marketplace UX requirement: if the OpenClaw config does not already contain
`plugins.entries.preloop-plugin.config`, the marketplace installer or a
separate Preloop connect helper must prompt the user to log in or sign up to
Preloop in a browser. Keep this OAuth/token bootstrap outside the runtime
extension entrypoint because OpenClaw blocks extension bundles that combine
environment access with credential-bearing network requests. The bootstrap
should use the existing OAuth CLI flow (`client_id=cli`,
`redirect_uri=urn:ietf:wg:oauth:2.0:oob`) to obtain a Preloop API token, call the
runtime-session bootstrap endpoint for the current OpenClaw runtime, then write
`plugins.entries.preloop-plugin.config` with the generated runtime bearer
token. Users should never have to hand-author runtime bearer tokens.

Note: the OpenClaw plugin runtime id must be `preloop-plugin` (not
`openclaw-plugin`) because ClawHub treats runtime ids as globally unique and
`openclaw-plugin` is already claimed by another publisher.

## OpenCode Plugin

OpenCode has no central plugin marketplace: discovery is npm plus the
`plugin` array in `opencode.json`. There is no ClawHub step and no manifest
file. The only release artifact is the npm package.

Build and validate the npm package:

```bash
cd preloop/runtime-plugins/opencode-preloop
npm ci
npm run build
npm pack --dry-run
npm publish --access public --dry-run
```

Publish to npm:

```bash
npm publish --access public
```

Manual smoke test on a machine without the Preloop CLI:

```bash
npm install -g @preloop-ai/opencode-plugin
# add {"plugin": ["@preloop-ai/opencode-plugin"]} to ~/.config/opencode/opencode.json
preloop-opencode-plugin verify --config ~/.config/opencode/opencode.json
preloop-opencode-plugin run --config ~/.config/opencode/opencode.json
```

Marketplace UX requirement: if `~/.config/opencode/opencode.json` does not
already contain `preloop.control`, a separate Preloop connect helper must
prompt the user to log in or sign up to Preloop in a browser (same flow as
the Hermes/OpenClaw plugins): use the existing OAuth CLI flow
(`client_id=cli`, `redirect_uri=urn:ietf:wg:oauth:2.0:oob`) to obtain a
Preloop API token, call the runtime-session bootstrap endpoint for the
current OpenCode runtime, then write `preloop.control` with the generated
runtime bearer token. Users should never have to hand-author runtime bearer
tokens.

## Codex CLI sidecar

Codex CLI has no plugin marketplace. The sidecar is an npm package, same
shape as `@preloop-ai/claude-plugin`: install with npm, then run the bin.
The preloop-ee job `publish:codex-plugin` publishes it: manual, on the main
pipeline, after the preloop submodule bump lands on preloop-ee `main`. It
uses the shared `NPM_TOKEN`, refuses a version already on npm, and runs
`npm ci`, `npm run build`, and `npm test` before publish.
`publish-runtime-plugins.yml` still enumerates OpenClaw, Hermes, and the
harness plugin only (the Claude sidecar is not in that workflow either), so
the commands below remain the local fallback.

Versions are a single `package.json` field (there is no second manifest to
keep in lockstep). Confirm the name is `@preloop-ai/codex-plugin` and the
bin is `preloop-codex-plugin`.

Build and validate:

```bash
cd preloop/runtime-plugins/codex-preloop
npm ci
npm test
npm pack --dry-run
npm publish --access public --dry-run
```

Publish to npm:

```bash
npm publish --access public
```

Manual smoke test on a machine without the Preloop CLI:

```bash
npm install -g @preloop-ai/codex-plugin
preloop-codex-plugin verify --config ~/.codex/preloop-control.json
preloop-codex-plugin run --config ~/.codex/preloop-control.json
```

The control file is `~/.codex/preloop-control.json`. Do not write Codex
`config.toml` or `auth.json` from this package. If that file is missing, a
separate connect helper must obtain a Preloop API token and write
`preloop.control` (the same OAuth CLI flow the other plugins use:
`client_id=cli`, `redirect_uri=urn:ietf:wg:oauth:2.0:oob`). Users should
never have to hand-author runtime bearer tokens.

## Hermes Plugin

Hermes has no central plugin marketplace. Discovery is PyPI plus the correct
`hermes_agent.plugins` entry point so Hermes can load the package after
`pip install`.

Build and validate the Python package:

```bash
cd preloop/runtime-plugins/hermes-preloop
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m pip install --force-reinstall dist/*.whl
preloop-hermes-plugin verify --config ~/.hermes/config.yaml
```

Publish to PyPI:

```bash
python -m twine upload dist/*
```

Manual smoke test on a machine without the Preloop CLI:

```bash
pip install preloop-hermes-plugin
preloop-hermes-plugin login --config ~/.hermes/config.yaml
preloop-hermes-plugin verify --config ~/.hermes/config.yaml
preloop-hermes-plugin run --config ~/.hermes/config.yaml
```

If Hermes exposes a local plugin installer that wraps PyPI, the equivalent is:

```bash
hermes plugins install preloop-hermes-plugin
```

Marketplace UX requirement: if `~/.hermes/config.yaml` does not already contain
`preloop.control`, the plugin must prompt the user to log in or sign up to
Preloop in a browser. The standalone helper should use the existing OAuth CLI
flow (`client_id=cli`, `redirect_uri=urn:ietf:wg:oauth:2.0:oob`) to obtain a
Preloop API token, call the runtime-session bootstrap endpoint for the current
Hermes runtime, then write `preloop.control` with the generated runtime bearer
token. Users should never have to hand-author runtime bearer tokens.

## Nanobot Plugin

Nanobot has no central plugin marketplace. Discovery is PyPI plus the
`preloop-nanobot-plugin` console script, so a dedicated virtualenv can run
the package after `pip install`.

Keep `nanobot-preloop/pyproject.toml` and `nanobot-preloop/preloop-plugin.json`
on the same `version`. The publish job fails if those versions disagree or
if the version is already on PyPI.

Build and validate the Python package:

```bash
cd preloop/runtime-plugins/nanobot-preloop
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
```

Publish from the preloop-ee GitLab job `publish:nanobot-plugin` (manual, on
the main pipeline, after the submodule bump). It needs CI/CD variable
`NANOBOT_PYPI_TOKEN` (masked and protected). PyPI project-scoped tokens
cannot exist before the first release, so the first publish needs an
account-scoped token. Re-scope that token to the `preloop-nanobot-plugin`
project afterwards. Nanobot tests run in the preloop-ee job
`test:unit:runtime-plugins` (with
`PYTHONPATH=backend:runtime-plugins/nanobot-preloop/src`).
`publish:nanobot-plugin` needs that job and does not repeat the suite.
This repo's `.gitlab-ci.yml` job of the same name, and the GitHub Actions
job `test-runtime-plugins`, run the same suite.

Local fallback:

```bash
python -m twine upload dist/*
```

Install verification from PyPI, then the `preloop-nanobot-plugin` entry
point. `verify` and `run` read `~/.nanobot/preloop.json`, so enroll first.
Put `PRELOOP_ACCESS_TOKEN` in the environment. Do not pass it on the
command line. `verify` also checks that the installed SDK is
`nanobot-ai==0.1.4.post3` (the pin in `pyproject.toml` and
`preloop-plugin.json`).

```bash
pip install preloop-nanobot-plugin
preloop-nanobot-plugin enroll --base-url https://app.preloop.ai
preloop-nanobot-plugin verify
preloop-nanobot-plugin run
```

The package README documents a pinned virtualenv install from a checkout
instead of PyPI. Enrollment is the same step:

```bash
python3 -m venv ~/.local/share/preloop-nanobot/venv
~/.local/share/preloop-nanobot/venv/bin/pip install ./runtime-plugins/nanobot-preloop
preloop-nanobot-plugin enroll --base-url https://app.preloop.ai
preloop-nanobot-plugin verify
preloop-nanobot-plugin run
```

Run the package tests from the preloop repo root before publishing locally:

```bash
PRELOOP_DISABLE_TELEMETRY=true \
PYTHONPATH=backend:runtime-plugins/nanobot-preloop/src \
pytest runtime-plugins/nanobot-preloop/tests
```

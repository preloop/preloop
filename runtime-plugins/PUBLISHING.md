# Publishing Runtime Plugins

This guide covers publishing the standalone Preloop runtime plugins without
requiring the Preloop CLI on the target machine. The CLI can still provision
Agent Control config, but marketplace installation and runtime verification
must work from the agent runtime alone.

## Preferred Path: Manual GitLab CI Jobs

The enterprise repo pipeline (preloop-ee `.gitlab-ci.yml`) has manual publish
jobs mirroring `publish:langchain-preloop`:

- `publish:openclaw-plugin` — builds and publishes `@preloop-ai/openclaw-plugin`
  to npm (requires the `OPENCLAW_NPM_TOKEN` CI variable). ClawHub publication
  remains a manual follow-up (see below) because `clawhub login` is interactive.
- `publish:hermes-plugin` — builds and publishes `preloop-hermes-plugin` to
  PyPI (requires the `HERMES_PYPI_TOKEN` CI variable).

Both jobs fail fast if the package version and its plugin manifest version
disagree, or if the version is already on the registry. Release flow:

1. Bump the versions (see Release Preconditions below) in the preloop repo.
2. Land the preloop submodule bump on preloop-ee `main`.
3. On that pipeline, trigger the manual publish job(s) from the `deploy` stage.
4. For OpenClaw, run the ClawHub publish steps below as a follow-up.

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
- Confirm the package names are final:
  - npm/OpenClaw: `@preloop-ai/openclaw-plugin`
  - PyPI/Hermes: `preloop-hermes-plugin`
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

After npm publishing, publish the same package to ClawHub (the OpenClaw plugin
catalog). Install the CLI if needed (`npm install -g clawhub`), then:

```bash
# Resolve provenance from the preloop git checkout (submodule or clone):
SOURCE_REPO=https://github.com/preloop/preloop
SOURCE_COMMIT=$(git -C ../.. rev-parse HEAD)  # from openclaw-preloop/
SOURCE_PATH=runtime-plugins/openclaw-preloop

clawhub login  # if needed
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
```

`--source-repo` and `--source-commit` must be set together. Use the public
GitHub repo (`preloop/preloop`) and a commit SHA that exists there. Prefer
also setting `--source-path` to the monorepo subpath so ClawHub can locate the
plugin sources. The package scope (`@preloop-ai/...`) must match an existing
ClawHub publisher handle (`preloop-ai`).

The ClawHub/marketplace entry should install the npm package and run:

```bash
preloop-openclaw-plugin verify --config ~/.openclaw/openclaw.json
```

Manual smoke test on a machine without the Preloop CLI:

```bash
openclaw plugins install @preloop-ai/openclaw-plugin
# or, once listed on ClawHub:
# openclaw plugins install clawhub:@preloop-ai/openclaw-plugin
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

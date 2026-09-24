# Pi and DeepSeek Harness for Preloop

`@preloop-ai/harness-plugin` connects the Pi coding agent and DeepSeek Harness
(`dsh`) to Preloop. These are distinct agent kinds, `pi` and `deepseek`;
DeepSeek Harness can use any model served by the configured Preloop gateway.

## Onboarding

Install Node.js 22 or newer, authenticate the Preloop CLI, then onboard:

```sh
preloop agents install-runtime pi
preloop agents install-runtime deepseek
preloop agents onboard Pi --approvals
preloop agents onboard "DeepSeek Harness" --approvals
```

For existing installations, skip `install-runtime`. `preloop agents discover`
also detects the `pi` and `dsh` executables and native configuration markers.
`dsh` is accepted as a CLI alias for DeepSeek Harness. Select a model already
configured in Preloop with `--model <alias>`, or use the onboarding picker.
Pi's selected provider/model and API-key credentials can be imported from its
native settings, models, auth file, or environment. DeepSeek Harness's selected
provider/model and environment API keys can also be imported. OAuth credentials,
Pi executable secret expressions, and DeepSeek's encrypted credential store are
not imported by these adapters; select an existing Preloop model in those cases.

The CLI installs this package under `~/.preloop/runtime-plugins/harness` and
writes a private `preloop.json` in `~/.pi/agent` or `~/.dsh`. It respects
`PI_CODING_AGENT_DIR` and `DSH_HOME`. Pi loads an extension shim at
`extensions/preloop/index.ts`; DeepSeek loads marked plugin rows in the global
`cordis.patch.yml`. Native settings and unrelated plugins remain in place.
Restart the runtime after onboarding or `preloop agents refresh`.
`preloop agents offboard <name>` restores the managed configuration backup and
removes only Preloop's shim or marked patch rows.

## Tools, approvals and control

Pi's extension exposes the configured Preloop MCP tools. DeepSeek uses its
native Streamable HTTP MCP plugin. MCP tools remain subject to Preloop's tool
permissions and approval workflows.

`--approvals` additionally gates native tools before execution. `bash`, `read`,
`write`, and `edit` map to the existing Bash/Read/Write/Edit policy names, with
file paths normalized to `file_path`. Errors, cancellation, and invalid approval
responses deny execution. DeepSeek's local denials remain authoritative; a
monotonic guard prevents later middleware from overriding a Preloop denial.
Re-onboarding preserves the approvals setting.

Both plugins report session lifecycle events without uploading transcript text
or duplicating gateway token charges. Pi adds its native session ID to model
requests. DeepSeek reports native sessions through lifecycle hooks; its current
provider SDK does not offer per-request session headers for custom gateways.

An outbound authenticated WebSocket supports operator messages and interruption
of **active sessions**. Commands use native session identity, reject ambiguous
targets, and deduplicate replayed command IDs. Capability reporting explicitly
disables new sessions and voice. It does not reopen closed sessions or launch
another process. Flow workers use the existing flow cancellation controls;
interactive Talk connections belong to onboarded local runtimes.

## Ephemeral flows

Choose **Pi** or **DeepSeek Harness** in the flow form, or set `agent_type` to
`pi` or `deepseek` through the API. Docker, Kubernetes and private Docker runners
use the same bootstrap. Gateway credentials, allowed MCP tools, repository
setup, completion requirements and post-execution git operations are retained.
Set `agent_config.native_tool_approvals` to `true` to gate native tools as well as
MCP tools. Flow permission checks require the execution's own active runtime
credential. Native conversation checkpoint resume is not advertised.

Tested runtime pins are Pi `0.85.1` (`@earendil-works/pi-coding-agent`) and
DeepSeek Harness `0.1.5-rc.2`. DeepSeek is a developer preview; its main-branch
headless flags differ from the published release. `stdin.mjs` supplies its
headless startup service so even large prompts avoid command-line size limits.

Image builds install those versions with `npm ci` from `agents/pi` and
`agents/deepseek`. Bump a pin in `backend/preloop/agents/harness.py` and in
`agents/<name>/package.json` together, then regenerate that lockfile:

```sh
npm install --ignore-scripts --package-lock-only \
  --prefix runtime-plugins/harness-preloop/agents/pi
```

Every `resolved` entry needs an `integrity` hash. npm sometimes omits one on a
nested `@earendil-works/*` copy; copy it from
`npm view <package>@<version> dist.integrity`.

The default Node worker installs pinned packages at startup. For faster,
registry-independent launches, build the plugin and harness into an image:

```sh
docker build -f runtime-plugins/harness-preloop/Dockerfile \
  --build-arg HARNESS=pi -t preloop-pi .
docker build -f runtime-plugins/harness-preloop/Dockerfile \
  --build-arg HARNESS=deepseek -t preloop-deepseek .
```

Set `PI_IMAGE` / `DEEPSEEK_IMAGE` on workers, or `agent_config.image`. Approved
environment profiles must already contain the plugin and exact harness version.
Generic Docker workers initialize workspace ownership and drop to UID/GID 10000
before running the agent; Kubernetes uses its existing pod security context.
DeepSeek workers use the container as their sandbox, with native approval gates
still active when configured. Local onboarding preserves DeepSeek's OS sandbox.

## Development and release

```sh
cd runtime-plugins/harness-preloop
PRELOOP_DISABLE_TELEMETRY=true npm ci --ignore-scripts
PRELOOP_DISABLE_TELEMETRY=true npm test
```

The optional keyless integration test runs both real binaries against a local
mock model and approval server. It verifies a tool is blocked before executing:

```sh
PRELOOP_DISABLE_TELEMETRY=true DSH_TELEMETRY_DISABLED=true \
  PRELOOP_TEST_RUNTIME_BIN=/path/to/pinned-runtimes/node_modules/.bin \
  npm run test:runtime
```

After building images tagged `preloop-harness-pi:test` and
`preloop-harness-deepseek:test`, run the worker smoke tests from the repo root:

```sh
PRELOOP_DISABLE_TELEMETRY=true PRELOOP_TEST_HARNESS_DOCKER=1 \
  pytest backend/tests/agents/test_harness_docker_smoke.py -q
```

CLI development installs resolve this package from the repository checkout.
Release installations and generic worker bootstraps require
`@preloop-ai/harness-plugin@0.1.0` on npm. Publish through the existing runtime
plugin workflow (`plugin=harness`, or `harness-plugin-v0.1.0`) before distributing
CLI/backend releases that reference it. The Dockerfile embeds the checked-out
plugin directly and does not require that npm publication.

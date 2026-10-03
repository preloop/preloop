# Quick Start: Onboard Local Agents with the CLI

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The fastest way to get value out of Preloop is to install the Preloop CLI on the machine where your AI agents already run. The CLI discovers supported agents (Claude Code, Codex CLI, Gemini CLI, Hermes, OpenClaw, OpenCode, Cursor, Windsurf, ...), creates an account if you don't have one yet, provisions managed credentials/config, and rewrites supported tool and model endpoints through the Preloop MCP Firewall and Preloop Gateway.

Agent Control has one extra runtime requirement: for OpenClaw, Hermes, or another long-running agent to receive live operator commands, that agent must load a native Preloop runtime plugin. The CLI writes the `preloop.control` contract and can install or verify the plugin via `preloop agents install-plugin`; the plugin owns the long-lived WebSocket, reconnect/backoff, heartbeat/status events, capability advertisement, command receipt, and command execution.

!!! tip "60 second flow"
    1. Run the install command.
    2. The CLI scans your machine and lists every supported agent it found.
    3. It opens your browser to sign in or sign up.
    4. You're redirected back to **`/console/agents`** with everything wired up.

---

## Step 1: Install the Preloop CLI

Run this on macOS or Linux:

```bash
curl -fsSL https://preloop.ai/install/cli | sh
```

The installer:

1. Downloads the `preloop` binary into `/usr/local/bin` if it is writable, otherwise `~/.local/bin` (set `INSTALL_DIR` to choose another directory).
2. Runs `preloop agents discover --no-onboard-prompt` so you can see your detected agents **before** signing up.
3. Asks whether you want to log in (default), sign up, or skip authentication. Picking sign-up routes the browser to `/register` instead of `/login`.
4. After authentication completes, runs `preloop agents discover` to walk through onboarding for every detected agent (default `Y`).

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../assets/animations/quickstart/signup_flow.mp4" type="video/mp4">
  </video>
  <figcaption>One command: install, discover, sign up, onboard.</figcaption>
</figure>

!!! info "Unattended installs"
    Set `PRELOOP_CONFIRM=1` (truthy values: `1`, `y`, `yes`, `true`, `on`) to auto-approve every interactive prompt, both in the install script itself and in the CLI subcommands it invokes. This is the recommended way to bake Preloop into your provisioning scripts, devcontainers, or CI runners.

    ```bash
    PRELOOP_CONFIRM=1 curl -fsSL https://preloop.ai/install/cli | sh
    ```

---

## Step 2: Sign in or sign up

If this is your first time, choose **`s`** (sign up) at the prompt. The CLI opens a browser to `https://preloop.ai/register?…` and remembers the OAuth context it was about to consent to. After you create your account:

1. Preloop signs you in (no detour through the sign-in page).
2. The OAuth consent page resumes where it left off and authorizes the CLI.
3. Your browser lands on **`https://preloop.ai/console/agents?cli=connected`**, the live canvas of your account's agents.

If you already have an account, choose **`Y`** (default) and the same flow runs from `/login` instead.

!!! tip "SSH and headless hosts"
    On a headless host the CLI falls back to the copy/paste flow (`preloop login --headless`). Force it with `preloop login --headless` or `preloop signup --headless` to skip the loopback callback entirely. After pasting the code, the consent page shows a **Continue to your agents** button.

---

## Step 3: Confirm onboarding in the console

The **Agents** page lists every onboarded agent. The toolbar switches between the list, cards and canvas layouts; the canvas shows each agent connected to its tools and AI models, and you can pan and zoom it to inspect each runtime.

<figure>
  <img src="../../assets/animations/quickstart/dark/agents-onboarding.webp" alt="Preloop onboarding all detected agents and refreshing the agents canvas" style="width: 100%; border-radius: 8px;">
  <figcaption>Onboarding all detected agents refreshes the live agents canvas.</figcaption>
</figure>

Each agent shows its **onboarding state**:

| Badge | Meaning |
|-------|---------|
| `Fully onboarded` | The managed MCP entry and model gateway are configured. Only calls routed through that MCP entry reach Preloop. Agent Control still requires a loaded runtime plugin. |
| `MCP only` | The managed MCP entry is configured; no managed model gateway is configured. |
| `Gateway only` | The model gateway is configured; no managed MCP entry is configured. |
| `Incomplete` | Detected but not fully managed yet. Run `preloop agents onboard <name>` to connect it. |

An agent can also carry an **unverified** note ("Live check throttled, unverified" or "Upstream refused, unverified"): enrollment succeeded, but the direct gateway route/accounting probe was throttled or refused upstream. Retry it with `preloop agents validate <name> --live`. The probe does not launch the application; even a successful probe leaves application behavior unverified.

Batch onboarding continues past individual failures and ends with a per-agent summary table (`onboarded` / `partial` / `failed` plus a Reason). The command exits 0 when at least one agent onboarded fully or partially, see [CLI Reference](cli.md#the-onboarding-summary-table) for the exact semantics.

You can also start onboarding from **Onboard existing agent** at the top right of the page. The dialog shows the same CLI commands with copy buttons for users who want to step through manually.

---

## Step 4: Useful CLI commands

These are the commands the installer wires up for you. They also work standalone:

```bash
preloop login                  # OAuth login (loopback or copy/paste)
preloop signup                 # Same OAuth flow, but lands on the sign-up page
preloop auth status            # Show the active token + API URL
preloop auth logout            # Forget local credentials

preloop agents discover        # Detect local agents and walk through onboarding (Y/n per agent)
preloop agents discover --yes  # Onboard everything found, no prompts
preloop agents onboard <name>  # Explicitly onboard a known agent
preloop agents install-plugin <name>  # Install Agent Control runtime plugin (OpenClaw/Hermes)
preloop agents validate <name> # Verify MCP, gateway, and Agent Control readiness

preloop policy list            # List policies on your account
preloop policy validate <file> # Validate YAML before applying
preloop policy apply <file>    # Apply a policy
preloop approvals pending      # List approvals waiting on you
```

!!! note "What `discover` actually does"
    `preloop agents discover` reads each supported runtime's config (e.g. `~/.codex/config.toml`, `~/.claude/settings.json`, OpenClaw policies, Gemini CLI MCP servers, etc.), proposes any missing additions, and (once you confirm) rewrites supported entries to point at `https://preloop.ai/mcp/v1` (firewall) and the Preloop model gateway. It also writes Agent Control credentials/config for runtimes that support that contract. Original config is backed up next to it so you can roll back at any time.

!!! warning "Agent Control requires the runtime plugin"
    CLI onboarding alone cannot keep an agent online or execute operator commands. Run `preloop agents install-plugin <name>` (or install `@preloop-ai/openclaw-plugin` / `preloop-hermes-plugin` from the runtime marketplace), then restart the agent. Without the plugin loaded, MCP and gateway onboarding may be active, but the agent will not appear as a live Agent Control target and mobile/watch/web commands cannot reach the running session.

---

## What's Next?

<div class="grid cards" markdown>

-   :material-shield-key: **Layer policies on top**

    ---

    Now that your agents go through Preloop, set up allow / deny / approval rules.

    [**Continue to Part 1: Safety Layer**](quickstart.md)

-   :material-flow-chart: **Build agentic flows**

    ---

    Trigger your prelooped tools from event-driven workflows.

    [**Continue to Part 2: Flows**](quickstart-flows.md)

-   :material-cellphone: **Approve from your phone**

    ---

    Install the mobile app to approve risky tool calls on the go.

    [**Mobile apps**](clients/mobile-apps.md)

-   :material-console: **CLI reference**

    ---

    Full reference for every `preloop` subcommand and flag.

    [**CLI Reference**](cli.md)

</div>

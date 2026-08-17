# Preloop CLI

Command-line interface for managing AI agent policies, approvals, and MCP tools.

## Installation

### Installer (recommended)

macOS / Linux:

```sh
curl -fsSL https://preloop.ai/install/cli | sh
```

Windows (PowerShell):

```powershell
irm https://preloop.ai/install/cli.ps1 | iex
```

See [docs/windows-cli.md](../docs/windows-cli.md) for Defender false-positive
recovery and Git Bash `i686` architecture issues.

### From Source / `go install`

```bash
# Requires a Go toolchain — preferred on Windows when AV flags downloads
go install github.com/preloop/preloop/cli/cmd/preloop@latest

# Or clone and build
git clone https://github.com/preloop/preloop.git
cd preloop/cli
make install   # or: make build && ./build/preloop --help
```

### Pre-built Binaries

Download the latest release from [GitHub Releases](https://github.com/preloop/preloop/releases)
and verify against the `SHA256SUMS` asset.

## Quick Start

```bash
# Authenticate with a token
preloop login --token <your-token>

# Authenticate with a token and custom API URL
preloop login --token <your-token> --url http://localhost:8000

# OAuth login on a local machine
preloop login

# Create a Preloop account and authenticate the CLI in the same OAuth flow
preloop signup

# OAuth login over SSH or on a headless host
preloop login --headless

# OAuth login against a custom environment
PRELOOP_URL=https://review.preloop.ai preloop login --headless

# Check authentication status
preloop auth status

# List policies
preloop policy list

# Validate a policy file
preloop policy validate my-policy.yaml

# Apply a policy
preloop policy apply my-policy.yaml

# List pending approvals
preloop approvals pending

# Approve a request
preloop approvals approve <request-id>
```

## Commands

### Authentication

```bash
preloop login --token <token>        # Save an API token
preloop login                        # Auto-select loopback or headless OAuth
preloop login --headless             # Force copy/paste OAuth
preloop login --loopback             # Force local loopback OAuth
preloop signup                       # Open the sign-up page, then authenticate the CLI
preloop auth login                   # Same as preloop login
preloop auth signup                  # Same as preloop signup
preloop auth logout                  # Log out and clear credentials
preloop auth status                  # Show authentication status
preloop auth token                   # Print token for scripting
```

The login flow resolves the API URL in this order: `--url`, `PRELOOP_URL`, config file, then the default `https://preloop.ai`.

### Policy Management

```bash
preloop policy list                    # List all policies
preloop policy validate <file>         # Validate a policy file
preloop policy apply <file>            # Apply a policy
preloop policy apply <file> --dry-run  # Preview changes without applying
preloop policy diff <file>             # Compare local vs remote policy
preloop policy export <name>           # Export a policy to file
```

### MCP Tools

```bash
preloop tools list                               # List tools visible to this token
preloop tools describe <tool-name>              # Show schema and description
preloop tools exec <tool-name> --args '{"k":"v"}'
preloop tools exec <tool-name> --args-file ./input.json
```

`preloop tools` talks directly to the MCP endpoint, so the visible and executable tools are automatically filtered by the current token's policy. Agent tokens only see the tools they are allowed to use.

### Usage Import

```bash
preloop usage import cursor-usage.csv                # Cursor dashboard Usage export
preloop usage import events.json                     # Normalized usage events
preloop usage import cursor-usage.csv --agent-id <id>
preloop usage import export.csv --column-map '{"cost":"Cost to You"}'
```

Imports spend the model gateway never saw, such as Cursor's bundled models. Records are labeled as imported, so they are reported separately from gateway-metered spend and never count against gateway budgets. Re-importing the same file is safe: duplicates are detected and reported as skipped. Without `--agent-id`, the account's onboarded Cursor agent is used, so run `preloop agents onboard cursor` first or pass the id explicitly.

### Approvals

```bash
preloop approvals list                 # List all approvals
preloop approvals pending              # List pending approvals
preloop approvals approve <id>         # Approve a request
preloop approvals deny <id>            # Deny a request
```

### Agents

```bash
preloop agents discover                 # Interactive discovery; can prompt to onboard
preloop agents discover --json          # Emit discovery results as JSON
preloop agents discover --no-onboard-prompt
preloop agents discover --yes           # Auto-onboard newly discovered agents
preloop agents enroll openclaw        # Apply managed enrollment for OpenClaw
preloop agents enroll openclaw --dry-run
preloop agents enroll openclaw --yes   # Skip the confirmation prompt
preloop agents enroll hermes          # Apply managed enrollment for Hermes
preloop agents enroll hermes --dry-run
preloop agents install-runtime hermes # Install Hermes locally, then onboard
preloop agents install-runtime openclaw -y
preloop agents install-runtime hermes --skip-install -y  # Onboard an existing install
preloop agents status openclaw         # Show local/remote managed state
preloop agents status hermes
preloop agents validate openclaw       # Validate the managed config
preloop agents validate hermes
preloop agents restore openclaw        # Restore the most recent local backup
preloop agents restore hermes
preloop agents offboard openclaw       # Offboard and restore the local backup
preloop agents offboard hermes
preloop agents offboard openclaw --yes --remove-model no --remove-mcp-servers no
preloop agents offboard openclaw --yes --remove-model yes
```

`preloop agents discover` is the starting point for agent onboarding. In interactive terminals it can prompt to onboard newly discovered agents one by one. Use `--no-onboard-prompt` to keep discovery read-only in scripts/CI, or `--yes` to auto-onboard all new candidates. `preloop agents enroll openclaw` remains the explicit mutating command.

Managed OpenClaw and Hermes onboarding creates a durable managed credential, backs up the local config, adds or replaces the local MCP config with a managed `preloop` entry, writes a `preloop.control.control_ws_url` contract plus the standalone runtime plugin package name (`preloop-hermes-plugin` or `@preloop-ai/openclaw-plugin`), and may also import existing MCP servers plus rewrite supported model settings to Preloop's OpenAI-compatible gateway. Use `--dry-run` to preview changes first. `preloop agents onboard --all -y` also ensures every discovered OpenClaw/Hermes runtime plugin available to the CLI is installed and verified, including agents that were already onboarded locally and would otherwise be skipped by the config rewrite step.

The CLI provisions credentials and configuration, and `preloop agents install-plugin <agent>` delegates to the runtime's own plugin marketplace installer. The runtime plugin, not the CLI, owns the long-lived WebSocket connection to `/api/v1/agents/control/ws`, reconnect/backoff, heartbeat and status events, capability advertisement, command receipt, and command execution or message injection into the active agent session. Runtime builds that have not loaded the native Agent Control plugin can ignore the control block safely; MCP firewall and gateway routing can still work, but Agent Control is not enabled. `preloop agents validate` reports `control_config_written`, `control_plugin_installed`, `control_plugin_verified`, and `control_channel_configured` separately so a metadata block is not mistaken for a live control channel.

`preloop agents offboard` restores the last local backup and removes the managed agent from Preloop. Cleanup of account-level resources is controlled separately:

- `--remove-model ask|yes|no` controls whether an eligible AI model should also be removed from Preloop
- `--remove-mcp-servers ask|yes|no` controls whether eligible MCP servers should also be removed from Preloop

Both flags default to `ask`. With `--yes` alone, the CLI skips the main offboard confirmation but keeps eligible AI models and MCP servers unless you explicitly opt into removing them. Shared resources are protected automatically:

- AI models are kept if they are still referenced by another managed agent or by any flow
- MCP servers are kept if they are still referenced by another managed agent
- Recently active shared resources are also skipped

### Version

```bash
preloop version                        # Show version info
preloop version --check                # Check for updates
preloop update                         # Install the latest CLI release
preloop update --check                 # Print current vs latest and exit
preloop update --yes                   # Install without prompting
```

`preloop update` downloads the GitHub release asset for this OS/architecture
(the same URL as `scripts/install-cli.sh`) and replaces the current binary
in place. The daily update notice asks whether to upgrade when stdin is a
TTY and the binary is writable; otherwise it stays silent. Version lookup
is skipped when `PRELOOP_DISABLE_TELEMETRY` is set.

## Configuration

The CLI stores configuration in `~/.preloop/config.yaml`:

```yaml
access_token: <your-access-token>
refresh_token: <your-refresh-token>
api_url: http://localhost:8000
```

### Global Flags

All commands accept these flags:

- `--token <token>` - Override the access token for this invocation
- `--url <url>` - Override the API base URL for this invocation
- `--verbose` / `-v` - Enable verbose output

### Environment Variables

- `PRELOOP_TOKEN` - Override the access token
- `PRELOOP_URL` - Override the API base URL

### Resolution Priority

Authentication and URL resolution use these rules:

1. Token: `--token`, then `PRELOOP_TOKEN`, then the config file.
2. API URL: `--url`, then `PRELOOP_URL`, then the config file, then `https://preloop.ai`.

## Development

### Prerequisites

- Go 1.22 or later
- Make

### Building

```bash
# Build for current platform
make build

# Cross-compile for all platforms
make build-all

# Run tests
make test

# Format code
make fmt

# Run linter
make lint
```

### Project Structure

```
cli/
├── cmd/
│   └── preloop/
│       └── main.go          # Entry point
├── internal/
│   ├── api/
│   │   └── client.go        # HTTP client for Preloop API
│   ├── config/
│   │   └── config.go        # Config management
│   ├── cmd/
│   │   ├── root.go          # Root command
│   │   ├── auth.go          # auth login/logout/status
│   │   ├── policy.go        # policy validate/apply/diff/export/list
│   │   ├── tools.go         # tools list/describe/exec
│   │   ├── approvals.go     # approvals list/pending/approve/deny
│   │   ├── version.go       # version command
│   │   └── update.go        # update command
│   ├── mcpclient/
│   │   └── client.go        # Minimal MCP HTTP client
│   └── version/
│       ├── check.go         # Daily version check logic
│       └── update.go        # In-place GitHub release installer
├── go.mod
├── go.sum
├── Makefile
└── README.md
```

## License

Apache License 2.0. See `../LICENSE`.

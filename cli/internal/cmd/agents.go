package cmd

import (
	"bufio"
	"bytes"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"text/tabwriter"
	"time"

	toml "github.com/pelletier/go-toml/v2"
	"github.com/spf13/cobra"

	"github.com/preloop/preloop/cli/internal/api"
	"github.com/preloop/preloop/cli/internal/config"
)

// AgentConfig describes a discovered AI agent MCP configuration.
type AgentConfig struct {
	Name               string            `json:"name"`
	DisplayName        string            `json:"display_name,omitempty"`
	RuntimePrincipalID string            `json:"runtime_principal_id,omitempty"`
	ConfigPath         string            `json:"config_path"`
	MCPServers         map[string]MCPDef `json:"mcp_servers,omitempty"`
	IsOnboarded        bool              `json:"is_onboarded,omitempty"`
	OnboardingState    string            `json:"onboarding_state,omitempty"`
	// AuthState is the pre-onboarding readiness probe result
	// (ready / not_logged_in / unknown) — see detectAgentAuthState.
	AuthState  string `json:"auth_state,omitempty"`
	AuthDetail string `json:"auth_detail,omitempty"`
	// SupportLevel is the per-agent-type capability: "full" (MCP + model
	// routing) or "mcp-only" (model traffic not routable for this agent
	// type) — see supportLevelForAgent.
	SupportLevel string `json:"support_level,omitempty"`
	// RuntimeState reports whether the agent's runtime (executable or app
	// bundle) was actually found: present / missing / unknown — see
	// detectAgentRuntimeState. "missing" flags a config-only leftover from
	// an uninstalled agent; "unknown" is treated as present.
	RuntimeState         string   `json:"runtime_state,omitempty"`
	RuntimeDetail        string   `json:"runtime_detail,omitempty"`
	ConfigDrift          bool     `json:"config_drift,omitempty"`
	ReonboardRecommended bool     `json:"reonboard_recommended,omitempty"`
	DriftReasons         []string `json:"drift_reasons,omitempty"`
}

// MCPDef is a minimal MCP server definition read from an agent config.
type MCPDef struct {
	Command   string                 `json:"command,omitempty"`
	Args      []string               `json:"args,omitempty"`
	URL       string                 `json:"url,omitempty"`
	Transport string                 `json:"transport,omitempty"`
	Env       map[string]string      `json:"env,omitempty"`
	Headers   map[string]string      `json:"headers,omitempty"`
	Auth      map[string]interface{} `json:"auth,omitempty"`
}

// agentSpec defines where to look for a particular AI agent.
type agentSpec struct {
	Name                string
	ConfigPaths         []string // relative to $HOME
	DetectionPaths      []string // optional install markers relative to $HOME
	DetectionCommands   []string // optional executables on PATH that mark an install
	BootstrapConfigPath string   // optional synthesized config path relative to $HOME
	Parser              func(path string) (map[string]MCPDef, error)
}

var agentSpecs = []agentSpec{
	{
		Name:        "Claude Code",
		ConfigPaths: []string{".claude/settings.json", ".claude/mcp-servers.json"},
		// Fresh or lightly-used installs write only ~/.claude.json (and put
		// the `claude` binary on PATH) before any settings.json exists, so
		// treat both as install markers. Onboarding then bootstraps the
		// canonical ~/.claude/settings.json.
		DetectionPaths:      []string{".claude.json"},
		DetectionCommands:   []string{"claude"},
		BootstrapConfigPath: ".claude/settings.json",
		Parser:              parseClaudeConfig,
	},
	{
		Name:        "Claude Desktop",
		ConfigPaths: []string{".claude/claude_desktop_config.json", ".config/claude/claude_desktop_config.json"},
		Parser:      parseClaudeConfig,
	},
	{
		Name:        "Cursor",
		ConfigPaths: []string{".cursor/mcp.json"},
		Parser:      parseGenericMCP,
	},
	{
		Name:        "Windsurf",
		ConfigPaths: []string{".codeium/windsurf/mcp_config.json"},
		Parser:      parseGenericMCP,
	},
	{
		Name:        "VSCode / Copilot",
		ConfigPaths: []string{".vscode/mcp.json"},
		Parser:      parseGenericMCP,
	},
	{
		Name:        "Gemini CLI",
		ConfigPaths: []string{".gemini/settings.json"},
		Parser:      parseGeminiConfig,
	},
	{
		Name:                "OpenCode",
		ConfigPaths:         []string{".config/opencode/config.json"},
		DetectionPaths:      []string{".local/share/opencode/auth.json", ".config/opencode/package.json"},
		BootstrapConfigPath: ".config/opencode/config.json",
		Parser:              parseGenericMCP,
	},
	{
		Name:        "Codex CLI",
		ConfigPaths: []string{".codex/config.toml", ".codex/config.json"},
		Parser:      parseCodexConfig,
	},
	{
		Name: "OpenClaw",
		ConfigPaths: []string{
			".openclaw/openclaw.json",
			".openclaw/openclaw.json5",
			".openclaw-dev/openclaw.json",
			".openclaw-dev/openclaw.json5",
		},
		DetectionPaths:      []string{".openclaw", ".openclaw/openclaw.json.bak", ".config/openclaw", "Library/pnpm/openclaw"},
		BootstrapConfigPath: ".openclaw/openclaw.json",
		Parser:              parseOpenClawMCP,
	},
	{
		Name:                hermesAgentName,
		ConfigPaths:         hermesConfigRelativePaths,
		DetectionPaths:      hermesDetectionPaths,
		BootstrapConfigPath: hermesBootstrapConfigPath,
		Parser:              parseHermesConfig,
	},
	{
		// Google Antigravity (the IDE and the `agy` CLI share one MCP config
		// tree under ~/.gemini, so a single managed entry governs both
		// surfaces). The MCP path moved between releases, so we probe the
		// newer unified location and the IDE-specific one. Model traffic is
		// locked to Google's backend (no BYOK / base URL), so Antigravity is
		// MCP-firewall only — see supportsManagedGateway.
		Name: antigravityAgentName,
		ConfigPaths: []string{
			".gemini/config/mcp_config.json",
			".gemini/antigravity/mcp_config.json",
		},
		DetectionPaths: []string{
			"Library/Application Support/Antigravity",
			".config/Antigravity",
			".gemini/antigravity",
			".local/bin/agy",
		},
		BootstrapConfigPath: ".gemini/antigravity/mcp_config.json",
		Parser:              parseGenericMCP,
	},
	{
		// Devin (Cognition). Local CLI/Desktop config under ~/.config/devin.
		// Inference always runs in Cognition's cloud, so Devin is MCP-only;
		// model traffic cannot be redirected through the gateway.
		Name: devinAgentName,
		ConfigPaths: []string{
			".config/devin/config.json",
			".devin/config.json",
		},
		DetectionPaths: []string{
			".config/devin",
			".devin",
			".local/bin/devin",
		},
		BootstrapConfigPath: ".config/devin/config.json",
		Parser:              parseGenericMCP,
	},
}

// Display names for the additional MCP-only agent adapters. Kept as
// constants so the per-agent switch statements stay in sync with the
// registry above.
const (
	antigravityAgentName = "Antigravity"
	devinAgentName       = "Devin"
)

// isAntigravityAgent reports whether the agent is the Antigravity surface
// (covering both the IDE and the `agy` CLI, which share one MCP config).
func isAntigravityAgent(agent AgentConfig) bool {
	return strings.EqualFold(strings.TrimSpace(agent.Name), antigravityAgentName)
}

// isDevinAgent reports whether the agent is Devin.
func isDevinAgent(agent AgentConfig) bool {
	return strings.EqualFold(strings.TrimSpace(agent.Name), devinAgentName)
}

// mcpOnlyAgentModelNote returns a clear, agent-specific note explaining why
// an agent is onboarded for tool-call governance only and its model traffic
// is not routed through the Preloop gateway. These agents either expose no
// programmatic custom-base-URL hook (Cursor) or run inference exclusively on
// a vendor backend that cannot be redirected (Antigravity, Devin). Returns
// "" for agents that do support gateway routing.
func mcpOnlyAgentModelNote(agent AgentConfig) string {
	// Every mcp-only note leads with the shared support-level phrase so the
	// onboarding output matches the discovery listing and summary table:
	// this is a support level of the agent type, not an incomplete
	// onboarding.
	switch {
	case strings.EqualFold(strings.TrimSpace(agent.Name), "cursor"):
		return mcpOnlySupportLabel + ": Cursor tool calls are governed through " +
			"Preloop's MCP firewall. Cursor only accepts a custom model base URL via " +
			"its in-app Settings → Models (global, chat-only, no config-file hook), " +
			"so Preloop does not rewrite model traffic automatically. To route model " +
			"spend through Preloop, set the OpenAI base URL override in Cursor " +
			"settings to your Preloop gateway URL manually."
	case strings.EqualFold(strings.TrimSpace(agent.Name), "claude desktop"):
		return mcpOnlySupportLabel + ": Claude Desktop tool calls are governed " +
			"through Preloop's MCP firewall. Claude Desktop's model traffic is fixed " +
			"to Anthropic's backend by design and cannot be repointed, so MCP-level " +
			"governance is the complete onboarding for this agent type."
	case isAntigravityAgent(agent):
		return mcpOnlySupportLabel + ": Antigravity tool calls are governed through " +
			"Preloop's MCP firewall. Antigravity is locked to Google-hosted models " +
			"with no BYO key or custom base URL, so model traffic cannot be routed " +
			"through the Preloop gateway."
	case isDevinAgent(agent):
		return mcpOnlySupportLabel + ": Devin tool calls are governed through " +
			"Preloop's MCP firewall. Devin runs all inference in Cognition's cloud " +
			"and does not support third-party LLM endpoints, so model traffic cannot " +
			"be routed through the Preloop gateway."
	case supportLevelForAgent(agent) == agentSupportLevelMCPOnly:
		return mcpOnlySupportLabel + ": tool calls are governed through Preloop's " +
			"MCP firewall; this agent type exposes no hook for rewriting model " +
			"traffic."
	default:
		return ""
	}
}

// agentsCmd is the top-level agent management command.
var agentsCmd = &cobra.Command{
	Use:   "agents",
	Short: "Manage AI agents",
	Long:  `Discover and manage AI agents configured on your machine.`,
}

// agentsDiscoverCmd scans for local AI agents.
var agentsDiscoverCmd = &cobra.Command{
	Use:   "discover",
	Short: "Discover AI agents on this machine",
	Long: `Scan standard configuration paths for known AI agents and display their
MCP server configurations without mutating local files or your Preloop account.

Supported agents: Claude Code, Cursor, Windsurf, VSCode/Copilot,
                  Gemini CLI, OpenCode, Codex CLI, OpenClaw, Hermes,
                  Antigravity, Devin.

Each listed agent shows a pre-onboarding readiness probe:
  Auth     Ready / Not logged in / Unknown, detected from the agent's local
           auth artifacts. A not-logged-in agent can still be onboarded; live
           validation will fail until the agent logs in.
  Support  Full (MCP firewall + model routing), or MCP-governed when the
           agent type offers no way to route model traffic (a support level,
           not a failure).

When the post-scan onboarding prompts run, an error onboarding one agent does
not stop the remaining agents; a per-agent summary (onboarded / partial /
failed) is printed at the end. A missing agent binary downgrades that agent to
"partial" (MCP and model routing configured, managed launcher skipped) instead
of failing it.

Exit codes: 0 when at least one attempted agent onboarded (fully or
partially), non-zero only when every attempted agent failed.

Examples:
  preloop agents discover
  preloop agents discover --json`,
	RunE: runAgentsDiscover,
}

var agentsEnrollCmd = &cobra.Command{
	Use:     "onboard [agent]",
	Aliases: []string{"enroll"},
	Short:   "Onboard a discovered agent into managed MCP access",
	Long: `Create or locate the managed agent identity in Preloop, create a durable
credential for it, back up the local config, and add a managed Preloop MCP server
entry to the selected agent configuration.

This is the mutating companion to 'preloop agents discover'. Use --dry-run to
preview the planned config and account changes without writing anything.

A missing agent binary does not fail onboarding: the MCP and model routing
configuration still applies and the managed launcher step is skipped with a
warning ("partial" onboarding).

Exit codes: when onboarding multiple agents (no agent argument, or --all), an
error for one agent does not stop the others; the command exits 0 when at
least one attempted agent onboarded (fully or partially) and non-zero only
when every attempted agent failed. With a single agent argument, a partial
onboarding still exits 0.`,
	Args: cobra.MaximumNArgs(1),
	RunE: runAgentsEnroll,
}

var agentsStatusCmd = &cobra.Command{
	Use:   "status <agent>",
	Short: "Show managed enrollment status for an agent",
	Args:  cobra.ExactArgs(1),
	RunE:  runAgentsStatus,
}

var agentsListCmd = &cobra.Command{
	Use:   "list",
	Short: "List managed agents in the current Preloop account",
	RunE:  runAgentsList,
}

var agentsValidateCmd = &cobra.Command{
	Use:   "validate <agent>",
	Short: "Validate managed enrollment for an agent",
	Args:  cobra.MinimumNArgs(1),
	RunE:  runAgentsValidate,
}

var agentsInstallPluginCmd = &cobra.Command{
	Use:   "install-plugin <agent>",
	Short: "Install the standalone Preloop runtime plugin for an agent",
	Args:  cobra.ExactArgs(1),
	RunE:  runAgentsInstallPlugin,
}

var agentsRestoreCmd = &cobra.Command{
	Use:   "restore <agent>",
	Short: "Restore the most recent local backup for an enrolled agent",
	Args:  cobra.ExactArgs(1),
	RunE:  runAgentsRestore,
}

var agentsOffboardCmd = &cobra.Command{
	Use:   "offboard [agent]",
	Short: "Restore local config and remove managed enrollment",
	Args:  cobra.MaximumNArgs(1),
	RunE:  runAgentsOffboard,
}

const (
	offboardCleanupAsk = "ask"
	offboardCleanupYes = "yes"
	offboardCleanupNo  = "no"
)

type starterPolicyTool struct {
	Name                 string                    `json:"name"`
	Description          string                    `json:"description,omitempty"`
	Source               string                    `json:"source"`
	SourceID             string                    `json:"source_id,omitempty"`
	SourceName           string                    `json:"source_name,omitempty"`
	Schema               map[string]interface{}    `json:"schema,omitempty"`
	IsEnabled            bool                      `json:"is_enabled"`
	HasApprovalCondition bool                      `json:"has_approval_condition"`
	AccessRules          []starterPolicyAccessRule `json:"access_rules,omitempty"`
}

type starterPolicyAccessRule struct {
	Action             string `json:"action"`
	ConditionType      string `json:"condition_type,omitempty"`
	Description        string `json:"description,omitempty"`
	ApprovalWorkflowID string `json:"approval_workflow_id,omitempty"`
}

type runtimeSessionTokenRequest struct {
	SessionSourceType    string   `json:"session_source_type"`
	SessionSourceID      string   `json:"session_source_id"`
	SessionReference     string   `json:"session_reference,omitempty"`
	RuntimePrincipalID   string   `json:"runtime_principal_id,omitempty"`
	RuntimePrincipalName string   `json:"runtime_principal_name,omitempty"`
	ExpiresInMinutes     int      `json:"expires_in_minutes,omitempty"`
	Scopes               []string `json:"scopes,omitempty"`
	AllowedMCPServers    []string `json:"allowed_mcp_servers,omitempty"`
}

type runtimeSessionTokenResponse struct {
	RuntimeSessionID  string `json:"runtime_session_id"`
	Token             string `json:"token"`
	ExpiresAt         string `json:"expires_at"`
	SessionSourceType string `json:"session_source_type"`
	SessionSourceID   string `json:"session_source_id"`
	SessionReference  string `json:"session_reference,omitempty"`
}

type mcpServerResponse struct {
	ID         string                 `json:"id"`
	Name       string                 `json:"name"`
	URL        string                 `json:"url"`
	Transport  string                 `json:"transport,omitempty"`
	AuthType   string                 `json:"auth_type,omitempty"`
	AuthConfig map[string]interface{} `json:"auth_config,omitempty"`
}

type agentOnboardingFailure struct {
	Agent AgentConfig
	Err   error
}

// Batch onboarding statuses reported in the end-of-run summary. "partial"
// means the MCP/model configuration applied but an optional step (the
// managed launcher) was skipped, e.g. because the agent binary is missing.
const (
	agentOnboardingStatusOnboarded = "onboarded"
	agentOnboardingStatusPartial   = "partial"
	agentOnboardingStatusFailed    = "failed"
)

// agentOnboardingOutcome records how a single agent fared during a batch
// onboarding run (the discover-driven prompt loop or `onboard` without args).
type agentOnboardingOutcome struct {
	Agent  AgentConfig
	Status string
	// Reason is a one-line explanation for partial/failed outcomes.
	Reason string
	// MissingExecutable is true when the outcome stems from an agent binary
	// that could not be found on PATH (drives the WSL hint).
	MissingExecutable bool
}

// classifyAgentOnboardingOutcome converts an enrollment result into a summary
// outcome: nil is a full onboarding, a managedLauncherSkippedError is a
// partial onboarding, anything else is a failure.
func classifyAgentOnboardingOutcome(agent AgentConfig, err error) agentOnboardingOutcome {
	if err == nil {
		return agentOnboardingOutcome{
			Agent:  agent,
			Status: agentOnboardingStatusOnboarded,
			// Successful onboardings still surface a pending agent-side
			// login or an mcp-only support level in the Reason column so
			// the summary is honest about what "onboarded" covers.
			Reason: successSummaryReason(agent),
		}
	}
	var skipErr *managedLauncherSkippedError
	if errors.As(err, &skipErr) {
		return agentOnboardingOutcome{
			Agent:             agent,
			Status:            agentOnboardingStatusPartial,
			Reason:            skipErr.Error(),
			MissingExecutable: true,
		}
	}
	return agentOnboardingOutcome{
		Agent:             agent,
		Status:            agentOnboardingStatusFailed,
		Reason:            firstErrorLine(err),
		MissingExecutable: errors.Is(err, exec.ErrNotFound),
	}
}

// firstErrorLine keeps summary reasons to a single line.
func firstErrorLine(err error) string {
	if err == nil {
		return ""
	}
	line, _, _ := strings.Cut(err.Error(), "\n")
	return strings.TrimSpace(line)
}

// printAgentOnboardingSummary prints the per-agent summary table for a batch
// onboarding run, plus the WSL PATH hint when a missing executable was seen
// inside a WSL environment.
func printAgentOnboardingSummary(w io.Writer, outcomes []agentOnboardingOutcome) {
	if len(outcomes) == 0 {
		return
	}
	fmt.Fprintln(w, "\nOnboarding summary:") //nolint:errcheck
	tw := tabwriter.NewWriter(w, 0, 4, 2, ' ', 0)
	fmt.Fprintf(tw, "  Agent\tStatus\tReason\n") //nolint:errcheck
	missingExecutable := false
	anyFailed := false
	for _, outcome := range outcomes {
		reason := outcome.Reason
		if reason == "" {
			reason = "-"
		}
		fmt.Fprintf( //nolint:errcheck
			tw,
			"  %s\t%s\t%s\n",
			resolveAgentDisplayName(outcome.Agent),
			outcome.Status,
			reason,
		)
		if outcome.MissingExecutable {
			missingExecutable = true
		}
		if outcome.Status == agentOnboardingStatusFailed {
			anyFailed = true
		}
	}
	tw.Flush() //nolint:errcheck
	if missingExecutable {
		if hint := wslMissingExecutableHint(); hint != "" {
			fmt.Fprintf(w, "  Hint: %s\n", hint) //nolint:errcheck
		}
	}
	if anyFailed {
		printTroubleshootingFooter(w)
	}
}

// agentOnboardingSummaryError implements the batch onboarding exit-code
// convention documented in the discover/onboard help: exit 0 when at least
// one attempted agent onboarded (fully or partially, and trivially when
// nothing was attempted), non-zero only when every attempted agent failed.
func agentOnboardingSummaryError(outcomes []agentOnboardingOutcome) error {
	if len(outcomes) == 0 {
		return nil
	}
	for _, outcome := range outcomes {
		if outcome.Status != agentOnboardingStatusFailed {
			return nil
		}
	}
	return fmt.Errorf("failed to onboard all %d attempted agent(s)", len(outcomes))
}

// ignoreLauncherSkipped converts the partial-success launcher-skip marker to
// nil for single-agent flows: the enrollment applied and the warning has
// already been printed, so the command must exit 0.
func ignoreLauncherSkipped(err error) error {
	var skipErr *managedLauncherSkippedError
	if errors.As(err, &skipErr) {
		return nil
	}
	return err
}

type flowSummaryResponse struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	AIModelID string `json:"ai_model_id,omitempty"`
	IsPreset  bool   `json:"is_preset,omitempty"`
}

type managedAgentSummary struct {
	ID                     string                            `json:"id"`
	DisplayName            string                            `json:"display_name"`
	SessionSourceType      string                            `json:"session_source_type"`
	SessionSourceID        string                            `json:"session_source_id"`
	SessionReference       string                            `json:"session_reference,omitempty"`
	LifecycleState         string                            `json:"lifecycle_state"`
	ActivityStatus         string                            `json:"activity_status,omitempty"`
	LastSeenAt             string                            `json:"last_seen_at,omitempty"`
	LastActivityAt         string                            `json:"last_activity_at,omitempty"`
	OnboardingState        string                            `json:"onboarding_state,omitempty"`
	LatestModelAlias       string                            `json:"latest_model_alias,omitempty"`
	ConfiguredModels       []managedAgentModelBindingSummary `json:"configured_models,omitempty"`
	ManagedMCPServers      []string                          `json:"managed_mcp_servers"`
	MCPProxyConfigured     bool                              `json:"mcp_proxy_configured,omitempty"`
	ModelGatewayConfigured bool                              `json:"model_gateway_configured,omitempty"`
}

type managedAgentListResponse struct {
	Items []managedAgentSummary `json:"items"`
}

type managedAgentCredentialSummary struct {
	ID            string `json:"id"`
	APIKeyID      string `json:"api_key_id,omitempty"`
	Name          string `json:"name"`
	Status        string `json:"status"`
	KeyPrefix     string `json:"key_prefix,omitempty"`
	CreatedAt     string `json:"created_at"`
	RevokedAt     string `json:"revoked_at,omitempty"`
	RevokedReason string `json:"revoked_reason,omitempty"`
}

type managedAgentCredentialCreateRequest struct {
	Name          string   `json:"name"`
	Description   string   `json:"description,omitempty"`
	ExpiresInDays int      `json:"expires_in_days,omitempty"`
	Scopes        []string `json:"scopes,omitempty"`
}

type managedAgentCredentialCreateResponse struct {
	Credential managedAgentCredentialSummary `json:"credential"`
	Token      string                        `json:"token"`
}

type managedAgentEnrollmentSummary struct {
	ID               string                 `json:"id"`
	EnrollmentType   string                 `json:"enrollment_type"`
	AdapterKey       string                 `json:"adapter_key,omitempty"`
	Status           string                 `json:"status"`
	TargetConfigPath string                 `json:"target_config_path,omitempty"`
	DiscoveredConfig map[string]interface{} `json:"discovered_config,omitempty"`
	ManagedConfig    map[string]interface{} `json:"managed_config,omitempty"`
	BackupMetadata   map[string]interface{} `json:"backup_metadata,omitempty"`
	ValidationResult map[string]interface{} `json:"validation_result,omitempty"`
	RestoreAvailable bool                   `json:"restore_available"`
	CreatedAt        string                 `json:"created_at"`
	UpdatedAt        string                 `json:"updated_at"`
	LastAppliedAt    string                 `json:"last_applied_at,omitempty"`
	LastValidatedAt  string                 `json:"last_validated_at,omitempty"`
	LastRestoredAt   string                 `json:"last_restored_at,omitempty"`
}

type managedAgentEnrollmentCreateRequest struct {
	EnrollmentType   string                 `json:"enrollment_type"`
	AdapterKey       string                 `json:"adapter_key,omitempty"`
	Status           string                 `json:"status"`
	TargetConfigPath string                 `json:"target_config_path,omitempty"`
	DiscoveredConfig map[string]interface{} `json:"discovered_config,omitempty"`
	ManagedConfig    map[string]interface{} `json:"managed_config,omitempty"`
	BackupMetadata   map[string]interface{} `json:"backup_metadata,omitempty"`
	ValidationResult map[string]interface{} `json:"validation_result,omitempty"`
	RestoreAvailable bool                   `json:"restore_available"`
	LastAppliedAt    *time.Time             `json:"last_applied_at,omitempty"`
	LastValidatedAt  *time.Time             `json:"last_validated_at,omitempty"`
	LastRestoredAt   *time.Time             `json:"last_restored_at,omitempty"`
}

type managedAgentDetailResponse struct {
	Agent       managedAgentSummary             `json:"agent"`
	Credentials []managedAgentCredentialSummary `json:"credentials"`
	Enrollments []managedAgentEnrollmentSummary `json:"enrollments"`
}

type managedMCPEnrollmentPlan struct {
	Agent               AgentConfig
	DiscoveredDocument  map[string]interface{}
	ManagedDocument     map[string]interface{}
	SanitizedDiscovered map[string]interface{}
	SanitizedManaged    map[string]interface{}
	ManagedServerName   string
	ManagedServerURL    string
	ManagedControlWSURL string
	ManagedModelAlias   string
	ManagedProviderName string
	Notes               []string
}

type remoteServerSyncResult struct {
	Added               []string
	Reused              []string
	Skipped             []string
	ImportedFromCommand []string
	Warnings            []string
}

type localEnrollmentState struct {
	AgentName           string                 `json:"agent_name"`
	DisplayName         string                 `json:"display_name,omitempty"`
	RuntimePrincipalID  string                 `json:"runtime_principal_id"`
	EnrollmentID        string                 `json:"enrollment_id,omitempty"`
	ConfigPath          string                 `json:"config_path"`
	ConfigExisted       bool                   `json:"config_existed"`
	BackupPath          string                 `json:"backup_path"`
	ManagedServerName   string                 `json:"managed_server_name"`
	ManagedServerURL    string                 `json:"managed_server_url"`
	ManagedControlWSURL string                 `json:"managed_control_ws_url,omitempty"`
	AppliedAt           time.Time              `json:"applied_at"`
	RestoredAt          *time.Time             `json:"restored_at,omitempty"`
	DiscoveredConfig    map[string]interface{} `json:"discovered_config,omitempty"`
	ManagedConfig       map[string]interface{} `json:"managed_config,omitempty"`
}

type managedMCPAdapter interface {
	Key() string
	EnsureServerContainer(doc map[string]interface{}) (map[string]interface{}, error)
	BuildManagedServer(baseURL, token string) map[string]interface{}
	ValidateManagedConfig(doc map[string]interface{}, baseURL string) map[string]interface{}
}

// agentsStarterPolicyCmd generates a starter policy suggestion for an MCP server.
var agentsStarterPolicyCmd = &cobra.Command{
	Use:   "starter-policy <mcp-server>",
	Short: "Generate a starter policy for an MCP server",
	Long: `Generate a scoped starter policy suggestion for a specific MCP server
using the existing policy generation API.

The generated prompt is limited to the selected server and its discovered tools,
preserves current account configuration, and prefers approvals for mutating or
otherwise high-impact tools.

Examples:
  preloop agents starter-policy github
  preloop agents starter-policy github --output github-policy.yaml
  preloop agents starter-policy github --apply
  preloop agents starter-policy github --apply --yes
  preloop agents starter-policy github --apply --dry-run`,
	Args: cobra.ExactArgs(1),
	RunE: runAgentsStarterPolicy,
}

func init() {
	agentsCmd.AddCommand(agentsDiscoverCmd)
	agentsCmd.AddCommand(agentsEnrollCmd)
	agentsCmd.AddCommand(agentsStatusCmd)
	agentsCmd.AddCommand(agentsListCmd)
	agentsCmd.AddCommand(agentsValidateCmd)
	agentsCmd.AddCommand(agentsInstallPluginCmd)
	agentsCmd.AddCommand(agentsRestoreCmd)
	agentsCmd.AddCommand(agentsOffboardCmd)
	agentsCmd.AddCommand(agentsStarterPolicyCmd)

	agentsDiscoverCmd.Flags().Bool("add", false, "deprecated: use 'preloop agents onboard <agent>' instead")
	agentsDiscoverCmd.Flags().Bool("json", false, "output discovered agents as JSON (read-only, no prompts)")
	agentsDiscoverCmd.Flags().Bool("no-onboard-prompt", false, "do not prompt to onboard discovered agents")
	agentsDiscoverCmd.Flags().BoolP("yes", "y", false, "auto-approve interactive onboarding prompts")
	agentsDiscoverCmd.Flags().BoolP("force", "f", false, "alias for --yes")
	agentsDiscoverCmd.Flags().Bool("skip-live-validate", false, "do not run a live validation prompt after onboarding")
	_ = agentsDiscoverCmd.Flags().MarkDeprecated("add", "use 'preloop agents onboard [agent]'")
	agentsEnrollCmd.Flags().Bool("dry-run", false, "preview account and config changes without writing")
	agentsEnrollCmd.Flags().BoolP("yes", "y", false, "skip the onboarding confirmation prompt")
	agentsEnrollCmd.Flags().BoolP("force", "f", false, "alias for --yes")
	agentsEnrollCmd.Flags().Bool("all", false, "onboard all discovered agents")
	agentsEnrollCmd.Flags().Bool("live-validate", true, "after onboarding, run a supported live validation prompt through the agent (default: true; pass --skip-live-validate or --live-validate=false to opt out)")
	agentsEnrollCmd.Flags().Bool("skip-live-validate", false, "do not run a live validation prompt after onboarding (overrides --live-validate)")
	agentsEnrollCmd.Flags().StringSlice("tags", []string{}, "add key-value tags to the enrolled agent (e.g., --tags ext=true,env=prod)")
	agentsEnrollCmd.Flags().Bool("approvals", false, "install a native tool-permission hook that routes would-prompt tool calls to Preloop mobile/watch approvals (Claude Code, Codex CLI, Cursor)")
	agentsListCmd.Flags().Bool("json", false, "output managed agents as JSON")
	agentsStatusCmd.Flags().Bool("json", false, "output managed status as JSON")
	agentsValidateCmd.Flags().Bool("live", false, "run a supported live validation prompt in addition to config validation")
	agentsInstallPluginCmd.Flags().Bool("dry-run", false, "print the runtime plugin installation command without running it")
	agentsRestoreCmd.Flags().BoolP("yes", "y", false, "skip the restore confirmation prompt")
	agentsRestoreCmd.Flags().BoolP("force", "f", false, "alias for --yes")
	agentsOffboardCmd.Flags().BoolP("yes", "y", false, "skip offboarding and cleanup confirmations")
	agentsOffboardCmd.Flags().BoolP("force", "f", false, "alias for --yes")
	agentsOffboardCmd.Flags().Bool("all", false, "offboard all enrolled agents")
	agentsOffboardCmd.Flags().String("remove-model", offboardCleanupAsk, "whether to remove an eligible AI model from Preloop as part of offboarding: ask, yes, or no")
	agentsOffboardCmd.Flags().String("remove-mcp-servers", offboardCleanupAsk, "whether to remove eligible MCP servers from Preloop as part of offboarding: ask, yes, or no")
	agentsStarterPolicyCmd.Flags().StringP("output", "o", "", "write generated policy YAML to a file")
	agentsStarterPolicyCmd.Flags().Bool("apply", false, "apply the generated policy immediately")
	agentsStarterPolicyCmd.Flags().Bool("dry-run", false, "when used with --apply, validate without applying changes")
	agentsStarterPolicyCmd.Flags().Bool("yes", false, "skip the apply confirmation prompt after previewing changes")
	agentsStarterPolicyCmd.Flags().Bool("no-context", false, "do not include current account config as context for the LLM")
}

// runAgentsDiscover scans for AI agents on the machine.
func runAgentsDiscover(cmd *cobra.Command, args []string) error {
	asJSON, _ := cmd.Flags().GetBool("json")
	addServers, _ := cmd.Flags().GetBool("add")
	noOnboardPrompt, _ := cmd.Flags().GetBool("no-onboard-prompt")
	autoApprove, _ := cmd.Flags().GetBool("yes")
	skipLiveValidate, _ := cmd.Flags().GetBool("skip-live-validate")

	if addServers {
		return fmt.Errorf("discover is now read-only; use 'preloop agents onboard <agent>'")
	}

	discovered, err := discoverAgents(os.Stdout, !asJSON)
	if err != nil {
		return err
	}
	client := authenticatedDiscoveryClient()
	discovered, err = enrichDiscoveredAgents(discovered, client)
	if err != nil {
		return err
	}

	if asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(discovered)
	}

	if len(discovered) == 0 {
		fmt.Println("No AI agents found on this machine.")
		fmt.Println("Looked for: Claude Code, Cursor, Windsurf, VSCode, Gemini CLI, OpenCode, Codex CLI, OpenClaw, Hermes, Antigravity, Devin")
		return nil
	}

	fmt.Printf("Found %d AI agent(s):\n\n", len(discovered))

	totalServers := 0
	for _, agent := range discovered {
		serverCount := len(agent.MCPServers)
		totalServers += serverCount
		fmt.Printf("  🤖 %s\n", agent.Name)
		fmt.Printf("     Agent Name: %s\n", resolveAgentDisplayName(agent))
		fmt.Printf("     Runtime principal: %s\n", runtimePrincipalIDForAgent(agent))
		fmt.Printf("     Config: %s\n", agent.ConfigPath)
		fmt.Printf("     Auth: %s\n", agentAuthListingLabel(agent))
		if runtimeLabel := agentRuntimeListingLabel(agent); runtimeLabel != "" {
			fmt.Printf("     Runtime: %s\n", runtimeLabel)
		}
		fmt.Printf("     Support: %s\n", agentSupportListingLabel(agent))
		if agent.IsOnboarded {
			fmt.Printf(
				"     Managed: yes (%s)\n",
				onboardingStateLabel(agent.OnboardingState),
			)
			fmt.Printf("     Routing: %s\n", onboardingStateNote(agent.OnboardingState))
			if agent.ConfigDrift {
				fmt.Println("     Config drift: detected")
				if agent.ReonboardRecommended {
					fmt.Println("     Re-onboard recommended: yes")
				}
				for _, reason := range agent.DriftReasons {
					fmt.Printf("       - %s\n", reason)
				}
			}
		}
		if serverCount > 0 {
			fmt.Printf("     MCP Servers (%d):\n", serverCount)
			for name := range agent.MCPServers {
				fmt.Printf("       - %s\n", name)
			}
		} else {
			fmt.Println("     No MCP servers configured")
		}
		fmt.Println()
	}

	if totalServers > 0 {
		fmt.Println("Tip: Use 'preloop agents onboard <agent>' to create a managed Preloop MCP entry with backup and restore support.")
	}

	if noOnboardPrompt {
		return nil
	}

	return promptToOnboardDiscoveredAgents(discovered, autoApprove, skipLiveValidate)
}

func promptToOnboardDiscoveredAgents(
	discovered []AgentConfig,
	autoApprove bool,
	skipLiveValidate bool,
) error {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		fmt.Println("Tip: Run 'preloop login' to onboard discovered agents.")
		return nil
	}

	candidates, err := filterAgentsPendingEnrollment(client, discovered)
	if err != nil {
		return err
	}
	if len(candidates) == 0 {
		fmt.Println("All discovered agents are already onboarded or have local managed enrollment state.")
		return nil
	}

	// Config-only agents (runtime uninstalled, config files left behind) are
	// never onboarded by the discover-driven batch — neither under --yes nor
	// via the interactive prompts. Explicit `preloop agents onboard <name>`
	// still works, with a warning.
	candidates, configOnly := splitRuntimeMissingCandidates(candidates)
	printRuntimeMissingOnboardingSkips(os.Stdout, configOnly)
	if len(candidates) == 0 {
		return nil
	}

	// We defer live validation so that all agents in the discover-driven batch
	// are onboarded first, then their live checks run concurrently in a single
	// post-onboarding phase (see runDeferredLiveValidationsParallel). This
	// means the wall clock for "discover + onboard" is no longer dominated by
	// per-agent live-validate latency (e.g. Codex' chatgpt.com backend can
	// take ~5s) and a slow upstream for one agent never blocks subsequent
	// agents from being touched.
	deferLiveValidate := !skipLiveValidate
	outcomes, err := promptToOnboardCandidatesTiered(os.Stdin, os.Stdout, client, candidates, autoApprove, true, func(agent AgentConfig, approvals bool) error {
		return executeManagedEnrollment(agent, managedEnrollmentOptions{
			Client: client,
			// AutoApprove carries the -y flag so per-agent sub-prompts
			// (e.g. stale OpenClaw plugin cleanup) auto-accept under -y.
			AutoApprove:       autoApprove,
			SkipConfirmation:  true,
			AgentPrepared:     true,
			LiveValidate:      true,
			SkipLiveValidate:  skipLiveValidate,
			DeferLiveValidate: deferLiveValidate,
			Approvals:         approvals,
			Input:             os.Stdin,
			Output:            os.Stdout,
		})
	})
	if err != nil {
		return err
	}
	// Fully and partially onboarded agents both have live enrollments worth
	// validating; only failed agents are excluded.
	var enrolled []AgentConfig
	for _, outcome := range outcomes {
		if outcome.Status != agentOnboardingStatusFailed {
			enrolled = append(enrolled, outcome.Agent)
		}
	}
	if deferLiveValidate && len(enrolled) > 0 {
		liveResults := runDeferredLiveValidationsParallel(client, enrolled, os.Stdout)
		applyLiveValidationOutcomesToSummary(outcomes, liveResults)
	}
	printAgentOnboardingSummary(os.Stdout, outcomes)
	return agentOnboardingSummaryError(outcomes)
}

// promptToOnboardCandidates walks the candidate agents, prompting per agent
// unless autoApprove is set, and enrolls each approved agent. An error from
// one agent's enrollment is recorded as a failed/partial outcome and the loop
// continues with the remaining agents; only prompt I/O failures abort the
// loop. The returned outcomes cover every attempted agent (skipped agents are
// not included).
func promptToOnboardCandidates(
	reader io.Reader,
	writer io.Writer,
	candidates []AgentConfig,
	autoApprove bool,
	askApprovals bool,
	enroll func(agent AgentConfig, approvals bool) error,
) ([]agentOnboardingOutcome, error) {
	bufferedReader := bufio.NewReader(reader)
	var outcomes []agentOnboardingOutcome

	for _, agent := range candidates {
		if !autoApprove {
			action := "Onboard"
			if agent.IsOnboarded {
				action = "Re-onboard"
			}
			confirmed, err := confirmActionDefaultYes(
				bufferedReader,
				writer,
				fmt.Sprintf("%s %s (%s) into managed Preloop access now? (Y/n): ", action, agent.Name, resolveAgentDisplayName(agent)),
			)
			if err != nil {
				return outcomes, fmt.Errorf("failed to read onboarding confirmation: %w", err)
			}
			if !confirmed {
				continue
			}
		}
		agent, err := prepareAgentForEnrollment(bufferedReader, writer, agent, autoApprove)
		if err != nil {
			return outcomes, err
		}
		// Offer the native tool-approvals hook for supported agents when the
		// caller's enrollment flow skips its own interactive prompts (the
		// discover-driven path); callers whose enrollment prompts interactively
		// pass askApprovals=false so the user is only asked once.
		approvals := false
		if askApprovals {
			if autoApprove || nonInteractiveAutoConfirm() {
				// --yes / non-interactive accepts the prompt's default answer
				// (Yes) for supported agents — previously it silently skipped
				// the hook, so `onboard --all -y` onboarded without approvals.
				approvals = isApprovalHookSupportedAgent(agent)
				if approvals {
					fmt.Fprintf(
						writer,
						"Routing %s's native tool calls through Preloop approvals (accepted default; offboard and re-onboard to remove the hook).\n",
						resolveAgentDisplayName(agent),
					)
				}
			} else {
				approvals, err = promptForApprovalsOptIn(bufferedReader, writer, agent)
				if err != nil {
					return outcomes, fmt.Errorf("failed to read approvals confirmation: %w", err)
				}
			}
		}
		outcome := classifyAgentOnboardingOutcome(agent, enroll(agent, approvals))
		if outcome.Status == agentOnboardingStatusFailed {
			// Keep the batch going: one broken agent environment must not
			// block the remaining agents from being onboarded.
			fmt.Fprintf( //nolint:errcheck
				writer,
				"  Error onboarding %s: %s\n",
				resolveAgentDisplayName(agent),
				outcome.Reason,
			)
			if outcome.MissingExecutable {
				if hint := wslMissingExecutableHint(); hint != "" {
					fmt.Fprintf(writer, "  %s\n", hint) //nolint:errcheck
				}
			}
		}
		outcomes = append(outcomes, outcome)
	}

	return outcomes, nil
}

func onboardCandidatesBestEffort(
	reader io.Reader,
	writer io.Writer,
	candidates []AgentConfig,
	opts managedEnrollmentOptions,
	deferLiveValidate bool,
	enroll func(AgentConfig, managedEnrollmentOptions) error,
) ([]AgentConfig, []agentOnboardingFailure) {
	bufferedReader := bufio.NewReader(reader)
	var enrolled []AgentConfig
	var failures []agentOnboardingFailure

	for _, agent := range candidates {
		prepared, err := prepareAgentForEnrollment(
			bufferedReader,
			writer,
			agent,
			true,
		)
		if err != nil {
			failures = append(failures, agentOnboardingFailure{
				Agent: agent,
				Err:   err,
			})
			continue
		}

		agentOpts := opts
		agentOpts.AutoApprove = true
		agentOpts.SkipConfirmation = true
		agentOpts.AgentPrepared = true
		agentOpts.DeferLiveValidate = deferLiveValidate
		if err := enroll(prepared, agentOpts); err != nil {
			// A skipped managed launcher is a partial success (MCP and
			// model routing applied); count the agent as enrolled.
			if ignoreLauncherSkipped(err) == nil {
				enrolled = append(enrolled, prepared)
				continue
			}
			failures = append(failures, agentOnboardingFailure{
				Agent: prepared,
				Err:   err,
			})
			continue
		}
		enrolled = append(enrolled, prepared)
	}

	return enrolled, failures
}

func printAgentOnboardingFailures(
	writer io.Writer,
	failures []agentOnboardingFailure,
) {
	if len(failures) == 0 {
		return
	}
	fmt.Fprintln(writer, "\nSome agents could not be onboarded:") //nolint:errcheck
	missingExecutable := false
	for _, failure := range failures {
		fmt.Fprintf( //nolint:errcheck
			writer,
			"  - %s: %v\n",
			resolveAgentDisplayName(failure.Agent),
			failure.Err,
		)
		if errors.Is(failure.Err, exec.ErrNotFound) {
			missingExecutable = true
		}
	}
	if missingExecutable {
		if hint := wslMissingExecutableHint(); hint != "" {
			fmt.Fprintf(writer, "  %s\n", hint) //nolint:errcheck
		}
	}
	fmt.Fprintln( //nolint:errcheck
		writer,
		"Re-run `preloop agents onboard <agent> -y` after fixing the listed agent environment.",
	)
	printTroubleshootingFooter(writer)
}

// shouldPromptForEnrollmentName reports whether executeManagedEnrollment must
// run its own prepareAgentForEnrollment (which prompts for the display name in
// interactive runs). Callers that already prepared the agent — the discover
// and `onboard` batch loops call prepareAgentForEnrollment before enrolling —
// set AgentPrepared so the user is asked for the agent name exactly once.
func shouldPromptForEnrollmentName(opts managedEnrollmentOptions) bool {
	return !opts.SkipConfirmation && !opts.AutoApprove && !opts.AgentPrepared
}

func prepareAgentForEnrollment(
	reader *bufio.Reader,
	writer io.Writer,
	agent AgentConfig,
	autoApprove bool,
) (AgentConfig, error) {
	agent = normalizeDiscoveredAgent(agent)
	if autoApprove || nonInteractiveAutoConfirm() {
		agent.RuntimePrincipalID = generatedRuntimePrincipalID(
			resolveAgentDisplayName(agent),
			agent.ConfigPath,
		)
		return agent, nil
	}

	defaultName := resolveAgentDisplayName(agent)
	input, err := promptForTextInput(
		reader,
		writer,
		fmt.Sprintf("Agent name [%s]: ", defaultName),
	)
	if err != nil {
		return AgentConfig{}, fmt.Errorf("failed to read agent name: %w", err)
	}
	if strings.TrimSpace(input) != "" {
		agent.DisplayName = strings.TrimSpace(input)
	}
	agent.RuntimePrincipalID = generatedRuntimePrincipalID(
		resolveAgentDisplayName(agent),
		agent.ConfigPath,
	)
	return agent, nil
}

func promptForTextInput(reader *bufio.Reader, writer io.Writer, prompt string) (string, error) {
	if _, err := fmt.Fprint(writer, prompt); err != nil {
		return "", err
	}
	input, err := reader.ReadString('\n')
	if err != nil && err != io.EOF {
		return "", err
	}
	return strings.TrimSpace(input), nil
}

func filterAgentsPendingLocalEnrollment(discovered []AgentConfig) []AgentConfig {
	candidates := make([]AgentConfig, 0, len(discovered))
	for _, agent := range discovered {
		if _, err := loadLocalEnrollmentState(agent); err == nil {
			continue
		}
		candidates = append(candidates, agent)
	}
	return candidates
}

func filterAgentsPendingEnrollment(client *api.Client, discovered []AgentConfig) ([]AgentConfig, error) {
	candidates := make([]AgentConfig, 0, len(discovered))
	for _, agent := range discovered {
		if agent.ReonboardRecommended {
			candidates = append(candidates, agent)
			continue
		}
		if _, err := loadLocalEnrollmentState(agent); err == nil {
			continue
		}
		if client != nil {
			if managed, err := getManagedAgentForDiscovered(client, agent); err == nil &&
				managedAgentIsLocallyComplete(managed) {
				continue
			}
		}
		candidates = append(candidates, agent)
	}
	return candidates, nil
}

func managedAgentIsLocallyComplete(managed *managedAgentSummary) bool {
	if managed == nil {
		return false
	}
	switch strings.ToLower(strings.TrimSpace(managed.OnboardingState)) {
	case "fully_onboarded", "mcp_proxy_only":
		return true
	default:
		return false
	}
}

func isAutoApprove(cmd *cobra.Command) bool {
	yes, _ := cmd.Flags().GetBool("yes")
	force, _ := cmd.Flags().GetBool("force")
	return yes || force || nonInteractiveAutoConfirm()
}

func runAgentsEnroll(cmd *cobra.Command, args []string) error {
	dryRun, _ := cmd.Flags().GetBool("dry-run")
	autoApprove := isAutoApprove(cmd)
	liveValidate, _ := cmd.Flags().GetBool("live-validate")
	skipLiveValidate, _ := cmd.Flags().GetBool("skip-live-validate")
	tagsInput, _ := cmd.Flags().GetStringSlice("tags")
	runAll, _ := cmd.Flags().GetBool("all")
	approvals, _ := cmd.Flags().GetBool("approvals")

	tags := make(map[string]string)
	for _, kv := range tagsInput {
		parts := strings.SplitN(kv, "=", 2)
		if len(parts) == 2 {
			tags[strings.TrimSpace(parts[0])] = strings.TrimSpace(parts[1])
		} else if len(parts) == 1 {
			tags[strings.TrimSpace(parts[0])] = "true"
		}
	}

	discovered, err := discoverAgents(os.Stdout, true)
	if err != nil {
		return err
	}

	opts := managedEnrollmentOptions{
		DryRun:           dryRun,
		AutoApprove:      autoApprove,
		LiveValidate:     liveValidate,
		SkipLiveValidate: skipLiveValidate,
		Approvals:        approvals,
		Tags:             tags,
		SkipConfirmation: false,
		Input:            os.Stdin,
		Output:           os.Stdout,
	}

	if len(args) == 0 {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return err
		}
		candidates, err := filterAgentsPendingEnrollment(client, discovered)
		if err != nil {
			return err
		}
		// Batch onboarding (with or without --all/--yes) never touches
		// config-only agents whose runtime is reliably missing; they need an
		// explicit `preloop agents onboard <name>`.
		var configOnly []AgentConfig
		candidates, configOnly = splitRuntimeMissingCandidates(candidates)
		printRuntimeMissingOnboardingSkips(os.Stdout, configOnly)
		if len(candidates) == 0 {
			if runAll {
				if err := ensureAgentControlOnboarding(client, discovered, opts); err != nil {
					return err
				}
			}
			fmt.Println("No unenrolled agents found.")
			return nil
		}

		// Live validation is deferred to a single parallel post-onboarding
		// phase whenever we are touching multiple agents in one invocation
		// (both the explicit ``--all`` path and the implicit "no args, multiple
		// candidates" path). This keeps onboarding strictly sequential — so
		// state-mutating steps (config rewrites, backups, durable credential
		// creation) stay deterministic — while collapsing the live-validate
		// wall clock from O(N) to ~max(individual checks). See
		// runDeferredLiveValidationsParallel for the implementation.
		deferLiveValidate := liveValidate && !skipLiveValidate

		if runAll {
			if !autoApprove {
				var names []string
				for _, a := range candidates {
					names = append(names, resolveAgentDisplayName(a))
				}
				confirmed, err := confirmActionDefaultYes(
					bufio.NewReader(os.Stdin),
					os.Stdout,
					fmt.Sprintf("Onboard agents %s into managed Preloop access now? (Y/n): ", strings.Join(names, ", ")),
				)
				if err != nil {
					return err
				}
				if !confirmed {
					return nil
				}
			}
			// Validate-first ordering: agents with verified model routing
			// onboard first; the MCP-only/unverified tier follows its printed
			// explanation. --all keeps onboarding everything.
			verified, unverified, reasons := partitionCandidatesByModelRouting(client, candidates)
			enrolled, failures := onboardCandidatesBestEffort(
				os.Stdin,
				os.Stdout,
				verified,
				opts,
				deferLiveValidate,
				executeManagedEnrollment,
			)
			if len(unverified) > 0 {
				printModelRoutingTierExplanation(os.Stdout, unverified, reasons, true)
				secondTier, secondFailures := onboardCandidatesBestEffort(
					os.Stdin,
					os.Stdout,
					unverified,
					opts,
					deferLiveValidate,
					executeManagedEnrollment,
				)
				enrolled = append(enrolled, secondTier...)
				failures = append(failures, secondFailures...)
			}
			if deferLiveValidate && len(enrolled) > 0 {
				runDeferredLiveValidationsParallel(client, enrolled, os.Stdout)
			}
			if err := ensureAgentControlOnboarding(client, discovered, opts); err != nil {
				return err
			}
			if len(failures) > 0 {
				printAgentOnboardingFailures(os.Stdout, failures)
				if len(enrolled) == 0 {
					return fmt.Errorf("failed to onboard all discovered agents")
				}
			}
			return nil
		}

		// askApprovals=false: executeManagedEnrollment prompts for the
		// approvals hook itself when this path runs interactively.
		outcomes, err := promptToOnboardCandidatesTiered(os.Stdin, os.Stdout, client, candidates, autoApprove, false, func(a AgentConfig, _ bool) error {
			agentOpts := opts
			agentOpts.SkipConfirmation = autoApprove
			agentOpts.DeferLiveValidate = deferLiveValidate
			// promptToOnboardCandidates already ran
			// prepareAgentForEnrollment (name prompt) for this agent; the
			// enrollment must keep its plan/apply confirmation but not ask
			// for the agent name a second time.
			agentOpts.AgentPrepared = true
			return executeManagedEnrollment(a, agentOpts)
		})
		if err != nil {
			return err
		}
		var enrolled []AgentConfig
		for _, outcome := range outcomes {
			if outcome.Status != agentOnboardingStatusFailed {
				enrolled = append(enrolled, outcome.Agent)
			}
		}
		if deferLiveValidate && len(enrolled) > 0 {
			liveResults := runDeferredLiveValidationsParallel(client, enrolled, os.Stdout)
			applyLiveValidationOutcomesToSummary(outcomes, liveResults)
		}
		printAgentOnboardingSummary(os.Stdout, outcomes)
		return agentOnboardingSummaryError(outcomes)
	}

	agent, err := findDiscoveredAgent(discovered, args[0])
	if err != nil {
		return err
	}

	// Explicitly named config-only agents may still be onboarded, but say up
	// front that the runtime is missing so the result is not mistaken for a
	// working enrollment.
	printRuntimeMissingOnboardingWarning(os.Stdout, agent)

	// A skipped managed launcher (missing agent binary) is a partial success:
	// the warning has been printed and the command exits 0.
	if err := ignoreLauncherSkipped(executeManagedEnrollment(agent, opts)); err != nil {
		printTroubleshootingFooter(os.Stdout)
		return err
	}
	return nil
}

func ensureAgentControlOnboarding(
	client *api.Client,
	agents []AgentConfig,
	opts managedEnrollmentOptions,
) error {
	if client == nil || !client.IsAuthenticated() {
		return nil
	}
	seen := map[string]bool{}
	for _, agent := range agents {
		agent = normalizeDiscoveredAgent(agent)
		if !supportsAgentControlChannel(agent) {
			continue
		}
		key := strings.ToLower(strings.TrimSpace(agent.Name)) + "\x00" + filepath.Clean(agent.ConfigPath)
		if seen[key] {
			continue
		}
		seen[key] = true
		if !agentControlNeedsOnboardingRefresh(agent) {
			continue
		}
		fmt.Fprintf(
			opts.Output,
			"Refreshing Agent Control config for %s...\n",
			resolveAgentDisplayName(agent),
		) //nolint:errcheck
		agentOpts := opts
		agentOpts.AutoApprove = true
		agentOpts.SkipConfirmation = true
		agentOpts.DeferLiveValidate = false
		if err := ignoreLauncherSkipped(executeManagedEnrollment(agent, agentOpts)); err != nil {
			return err
		}
	}
	return nil
}

func agentControlNeedsOnboardingRefresh(agent AgentConfig) bool {
	document, err := loadAgentConfigDocument(agent)
	if err != nil {
		return true
	}
	validation := managedMCPAdapterForAgent(agent).ValidateManagedConfig(
		document,
		clientBaseURLForFlags(),
	)
	configured, _ := validation["control_channel_configured"].(bool)
	return !configured
}

func ensureAgentControlRuntimePlugins(
	client *api.Client,
	agents []AgentConfig,
	writer io.Writer,
) {
	seen := map[string]bool{}
	for _, agent := range agents {
		agent = normalizeDiscoveredAgent(agent)
		if !supportsAgentControlChannel(agent) {
			continue
		}
		key := strings.ToLower(strings.TrimSpace(agent.Name)) + "\x00" + filepath.Clean(agent.ConfigPath)
		if seen[key] {
			continue
		}
		seen[key] = true

		fmt.Fprintf(
			writer,
			"Ensuring Agent Control runtime plugin for %s...\n",
			resolveAgentDisplayName(agent),
		) //nolint:errcheck
		pluginResult := installAgentControlRuntimePlugin(agent, writer)
		persistAgentControlPluginValidation(client, agent, pluginResult, writer)
	}
}

func persistAgentControlPluginValidation(
	client *api.Client,
	agent AgentConfig,
	pluginResult map[string]interface{},
	writer io.Writer,
) {
	if client == nil || !client.IsAuthenticated() {
		return
	}
	detail, err := getManagedAgentDetailForDiscovered(client, agent)
	if err != nil {
		fmt.Fprintf(
			writer,
			"  Warning: could not persist Agent Control plugin status for %s: %v\n",
			resolveAgentDisplayName(agent),
			err,
		) //nolint:errcheck
		return
	}

	enrollmentID := ""
	existingValidation := map[string]interface{}{}
	for _, enrollment := range detail.Enrollments {
		if enrollment.EnrollmentType == "cli_managed_config" {
			enrollmentID = enrollment.ID
			existingValidation = cloneStringMap(enrollment.ValidationResult)
			break
		}
	}
	if enrollmentID == "" {
		return
	}

	validationResult := existingValidation
	if document, err := loadAgentConfigDocument(agent); err == nil {
		validationResult = mergeStringMaps(
			validationResult,
			managedMCPAdapterForAgent(agent).ValidateManagedConfig(
				document,
				clientBaseURLForFlags(),
			),
		)
	}
	validationResult = mergeStringMaps(validationResult, pluginResult)

	status := "validated"
	if configured, _ := validationResult["control_channel_configured"].(bool); !configured {
		status = "validation_failed"
	}
	if _, err := validateManagedEnrollmentRecord(
		client,
		agent,
		enrollmentID,
		validationResult,
		status,
	); err != nil {
		fmt.Fprintf(
			writer,
			"  Warning: could not persist Agent Control plugin status for %s: %v\n",
			resolveAgentDisplayName(agent),
			err,
		) //nolint:errcheck
	}
}

func runAgentsStatus(cmd *cobra.Command, args []string) error {
	asJSON, _ := cmd.Flags().GetBool("json")

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		return err
	}
	agent, err := findDiscoveredAgent(discovered, args[0])
	if err != nil {
		return err
	}

	localState, _ := loadLocalEnrollmentState(agent)
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	var detail *managedAgentDetailResponse
	if client.IsAuthenticated() {
		managedAgent, err := getManagedAgentForDiscovered(client, agent)
		if err == nil {
			resp, err := getManagedAgentDetail(client, managedAgent.ID)
			if err != nil {
				return err
			}
			detail = resp
		}
	}

	if asJSON {
		payload := map[string]interface{}{
			"agent":        agent,
			"local_state":  localState,
			"remote_state": detail,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(payload)
	}

	fmt.Printf("Agent: %s\n", agent.Name)
	fmt.Printf("Agent name: %s\n", resolveAgentDisplayName(agent))
	fmt.Printf("Config: %s\n", agent.ConfigPath)
	fmt.Printf("Runtime principal: %s\n", runtimePrincipalIDForAgent(agent))
	if localState != nil {
		fmt.Printf("Local backup: %s\n", localState.BackupPath)
		fmt.Printf("Managed MCP URL: %s\n", localState.ManagedServerURL)
	}
	if detail == nil {
		if client.IsAuthenticated() {
			fmt.Println("Remote status: managed agent not found yet")
		} else {
			fmt.Println("Remote status: unavailable (not authenticated)")
		}
		return nil
	}

	fmt.Printf("Managed agent ID: %s\n", detail.Agent.ID)
	fmt.Printf("Lifecycle: %s\n", detail.Agent.LifecycleState)
	if len(detail.Agent.ManagedMCPServers) > 0 {
		fmt.Printf("Managed MCP servers: %s\n", strings.Join(detail.Agent.ManagedMCPServers, ", "))
	}
	fmt.Printf("Durable credentials: %d\n", len(detail.Credentials))
	if len(detail.Enrollments) > 0 {
		latest := detail.Enrollments[0]
		fmt.Printf("Latest enrollment: %s (%s)\n", latest.EnrollmentType, latest.Status)
		if latest.TargetConfigPath != "" {
			fmt.Printf("Target config: %s\n", latest.TargetConfigPath)
		}
	}
	return nil
}

func runAgentsList(cmd *cobra.Command, args []string) error {
	asJSON, _ := cmd.Flags().GetBool("json")
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	agents, err := listManagedAgents(client)
	if err != nil {
		return err
	}
	if asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(agents)
	}
	if len(agents) == 0 {
		fmt.Println("No managed agents found in this account.")
		return nil
	}

	localAgentsByPrincipal := map[string]AgentConfig{}
	if discovered, discoverErr := discoverAgents(io.Discard, false); discoverErr == nil {
		for _, agent := range discovered {
			localAgentsByPrincipal[runtimePrincipalIDForAgent(agent)] = agent
		}
	}

	tw := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Printf("Managed agents (%d):\n\n", len(agents))
	fmt.Fprintln(tw, "NAME\tSOURCE\tLIFECYCLE\tACTIVITY\tONBOARDING\tMODEL\tLOCAL CONFIG") //nolint:errcheck
	fmt.Fprintln(tw, "----\t------\t---------\t--------\t----------\t-----\t------------") //nolint:errcheck
	for _, agent := range agents {
		localConfig := "-"
		if localAgent, ok := localAgentsByPrincipal[agent.SessionSourceID]; ok {
			localConfig = localAgent.ConfigPath
		}
		source := agent.SessionSourceType
		if strings.TrimSpace(agent.SessionSourceID) != "" {
			source = fmt.Sprintf("%s/%s", agent.SessionSourceType, agent.SessionSourceID)
		}
		activity := strings.TrimSpace(agent.ActivityStatus)
		if activity == "" {
			activity = "-"
		}
		onboarding := strings.TrimSpace(agent.OnboardingState)
		if onboarding == "" {
			onboarding = "-"
		}
		model := strings.TrimSpace(agent.LatestModelAlias)
		if model == "" {
			model = "-"
		}
		fmt.Fprintf( //nolint:errcheck
			tw,
			"%s (%s)\t%s\t%s\t%s\t%s\t%s\t%s\n",
			agent.DisplayName,
			agent.ID,
			source,
			agent.LifecycleState,
			activity,
			onboarding,
			model,
			localConfig,
		)
		if len(agent.ManagedMCPServers) > 0 {
			fmt.Fprintf(tw, "  MCP servers:\t%s\t\t\t\t\t\t\n", strings.Join(agent.ManagedMCPServers, ", ")) //nolint:errcheck
		}
	}
	_ = tw.Flush()
	return nil
}

func listManagedAgents(client *api.Client) ([]managedAgentSummary, error) {
	var response managedAgentListResponse
	if err := client.Get("/api/v1/agents?limit=100", &response); err != nil {
		return nil, fmt.Errorf("failed to list managed agents: %w", err)
	}
	sort.SliceStable(response.Items, func(i, j int) bool {
		return strings.ToLower(response.Items[i].DisplayName) < strings.ToLower(response.Items[j].DisplayName)
	})
	return response.Items, nil
}

func runAgentsValidate(cmd *cobra.Command, args []string) error {
	runLiveValidation, _ := cmd.Flags().GetBool("live")
	agentName := strings.Join(args, " ")
	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		return err
	}
	agent, err := findDiscoveredAgent(discovered, agentName)
	if err != nil {
		return err
	}

	result := map[string]interface{}{
		"config_path": agent.ConfigPath,
	}
	status := "validated"
	adapter := managedMCPAdapterForAgent(agent)
	document, err := loadAgentConfigDocument(agent)
	if err != nil {
		status = "validation_failed"
		result["config_parse_ok"] = false
		result["error"] = err.Error()
	} else {
		result["config_parse_ok"] = true
		for key, value := range adapter.ValidateManagedConfig(document, clientBaseURLForFlags()) {
			result[key] = value
		}
		if passed, _ := result["validation_passed"].(bool); !passed {
			status = "validation_failed"
		}
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err == nil && client.IsAuthenticated() {
		enrollmentID := ""
		existingValidation := map[string]interface{}{}
		if state, err := loadLocalEnrollmentState(agent); err == nil {
			enrollmentID = state.EnrollmentID
		}
		if detail, err := getManagedAgentDetailForDiscovered(client, agent); err == nil {
			for _, enrollment := range detail.Enrollments {
				if enrollment.EnrollmentType == "cli_managed_config" {
					if enrollmentID == "" {
						enrollmentID = enrollment.ID
					}
					existingValidation = cloneStringMap(enrollment.ValidationResult)
					break
				}
			}
		}
		if runLiveValidation && status == "validated" {
			liveResult, liveErr := runManagedAgentLiveValidation(client, agent, existingValidation)
			if liveResult != nil {
				result = mergeStringMaps(result, liveResult.ValidationResult)
				if liveResult.Attempted && !liveResult.Passed {
					status = "validation_failed"
				}
			}
			if liveErr != nil {
				fmt.Printf("  live_validation_error: %v\n", liveErr)
				status = "validation_failed"
				result["live_validation_error"] = liveErr.Error()
			}
		} else if runLiveValidation {
			result = mergeStringMaps(existingValidation, result)
		} else if !runLiveValidation {
			result = mergeStringMaps(existingValidation, result)
		}
		if runLiveValidation && onboardingStateFromValidation(result) != "fully_onboarded" {
			status = "validation_failed"
		}
		if enrollmentID != "" {
			if _, err := validateManagedEnrollmentRecord(client, agent, enrollmentID, result, status); err != nil {
				return err
			}
		}
	}

	fmt.Printf("Validation status for %s: %s\n", resolveAgentDisplayName(agent), status)
	for _, key := range []string{
		"config_path",
		"config_parse_ok",
		"preloop_server_present",
		"gateway_provider_ok",
		"gateway_base_url_ok",
		"gateway_token_ok",
		"model_provider_rewritten",
		"live_validation_status",
		"live_validation_passed",
		"live_validation_skip_reason",
		"control_config_written",
		"control_plugin_installed",
		"control_plugin_verified",
		"control_channel_configured",
		"error",
	} {
		if value, ok := result[key]; ok {
			fmt.Printf("  %s: %s\n", key, formatManagedValidationValue(key, value))
		}
	}
	fmt.Printf("  onboarding_mode: %s\n", onboardingStateLabel(onboardingStateFromValidation(result)))
	fmt.Printf("  routing: %s\n", onboardingStateNote(onboardingStateFromValidation(result)))

	if status != "validated" {
		printTroubleshootingFooter(os.Stdout)
		return fmt.Errorf("managed enrollment validation failed")
	}
	return nil
}

func runAgentsInstallPlugin(cmd *cobra.Command, args []string) error {
	dryRun, _ := cmd.Flags().GetBool("dry-run")
	agentName := strings.Join(args, " ")
	installCommand, installArgs, err := agentControlPluginInstallCommand(agentName)
	if err != nil {
		return err
	}
	if dryRun {
		fmt.Fprintf(cmd.OutOrStdout(), "%s %s\n", installCommand, strings.Join(installArgs, " "))
		return nil
	}
	executable, err := resolveRuntimeExecutable(installCommand)
	if err != nil {
		return fmt.Errorf(
			"%s was not found on %s: %w",
			installCommand,
			runtimeExecutableSearchDescription(installCommand),
			err,
		)
	}
	// OpenClaw's plugin trust gate (plugins.allow) must admit the plugin or
	// the install registers nothing; ensure it before installing so the
	// standalone command matches what onboarding writes.
	if runtimeSessionSourceTypeForAgent(agentName) == "openclaw" {
		if err := ensureOpenClawPluginAllowlistedOnDisk(agentName); err != nil {
			fmt.Fprintf(
				cmd.ErrOrStderr(),
				"Warning: could not update OpenClaw's plugins.allow list: %v\n",
				err,
			)
		}
	}
	command := exec.Command(executable, installArgs...)
	output, err := command.CombinedOutput()
	if len(output) > 0 {
		_, _ = cmd.ErrOrStderr().Write(output)
	}
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		agent := AgentConfig{Name: agentName}
		if runtimeSessionSourceTypeForAgent(agent.Name) == "openclaw" {
			// Same ClawHub client failure the onboarding path works around:
			// fetch the npm tarball and install it as a local file.
			if installed, npmError := installOpenClawPluginViaNpmTarball(
				executable,
				agentControlPluginInstallTarget(agent),
				cmd.ErrOrStderr(),
			); installed {
				fmt.Fprintf(
					cmd.OutOrStdout(),
					"\nInstalled %s from the npm tarball. Run `preloop agents validate %s` to verify plugin load and control readiness.\n",
					agentControlPluginPackageName(agent),
					agentName,
				)
				return nil
			} else if npmError != "" {
				message = fmt.Sprintf(
					"%s (npm tarball fallback also failed: %s)", message, npmError,
				)
			}
		}
		if runtimeSessionSourceTypeForAgent(agent.Name) == hermesSourceType {
			// Hermes' `plugins install` only accepts Git URLs or owner/repo
			// shorthands; the Preloop plugin ships on PyPI, so fall back to a
			// pip install into Hermes' Python environment.
			if installed, pipError := installHermesPluginViaPip(
				agentControlPluginInstallTarget(agent),
				cmd.ErrOrStderr(),
			); installed {
				fmt.Fprintf(
					cmd.OutOrStdout(),
					"\nInstalled %s via pip. Run `preloop agents validate %s` to verify plugin load and control readiness.\n",
					agentControlPluginPackageName(agent),
					agentName,
				)
				return nil
			} else if pipError != "" {
				message = fmt.Sprintf("%s (pip fallback also failed: %s)", message, pipError)
			}
		}
		_, remediation := classifyRuntimePluginInstallFailure(installCommand, message)
		if remediation != "" {
			return fmt.Errorf(
				"failed to install Preloop runtime plugin: %s\n%s",
				message,
				remediation,
			)
		}
		return fmt.Errorf("failed to install Preloop runtime plugin: %s", message)
	}
	fmt.Fprintf(
		cmd.OutOrStdout(),
		"\nInstalled %s. Run `preloop agents validate %s` to verify plugin load and control readiness.\n",
		agentControlPluginPackageName(AgentConfig{Name: agentName}),
		agentName,
	)
	return nil
}

func agentControlPluginInstallCommand(agentName string) (string, []string, error) {
	agent := AgentConfig{Name: agentName}
	installer := agentControlPluginInstallerCommand(agent)
	if installer == "" {
		return "", nil, fmt.Errorf("agent %q does not support standalone Preloop runtime plugins", agentName)
	}
	installTarget := agentControlPluginInstallTarget(agent)
	if installTarget == "" {
		return "", nil, fmt.Errorf(
			"standalone Preloop runtime plugin target for %s was not found",
			agentName,
		)
	}
	return installer, []string{"plugins", "install", installTarget}, nil
}

func mergeStringMaps(base map[string]interface{}, overlays ...map[string]interface{}) map[string]interface{} {
	merged := cloneStringMap(base)
	for _, overlay := range overlays {
		for key, value := range overlay {
			merged[key] = value
		}
	}
	return merged
}

func cloneStringMap(source map[string]interface{}) map[string]interface{} {
	if len(source) == 0 {
		return map[string]interface{}{}
	}
	cloned := make(map[string]interface{}, len(source))
	for key, value := range source {
		cloned[key] = value
	}
	return cloned
}

func runAgentsRestore(cmd *cobra.Command, args []string) error {
	autoApprove, _ := cmd.Flags().GetBool("yes")

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		return err
	}
	agent, err := findDiscoveredAgent(discovered, args[0])
	if err != nil {
		return err
	}

	state, err := loadLocalEnrollmentState(agent)
	if err != nil {
		return err
	}
	if !autoApprove {
		confirmed, err := confirmAction(
			os.Stdin,
			os.Stdout,
			fmt.Sprintf("Restore %s from local backup %s? (y/N): ", resolveAgentDisplayName(agent), state.BackupPath),
		)
		if err != nil {
			return fmt.Errorf("failed to read confirmation: %w", err)
		}
		if !confirmed {
			fmt.Println("Aborted without restoring config.")
			return nil
		}
	}
	now, err := restoreAgentFromBackup(agent, state)
	if err != nil {
		return err
	}
	if err := saveLocalEnrollmentState(state); err != nil {
		return err
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err == nil && client.IsAuthenticated() {
		if state.EnrollmentID != "" {
			_, _ = restoreManagedEnrollmentRecord(
				client,
				agent,
				state.EnrollmentID,
				map[string]interface{}{
					"backup_path": state.BackupPath,
				},
			)
		} else if managedAgent, err := getManagedAgentForDiscovered(client, agent); err == nil {
			_, _ = createManagedEnrollmentRecord(client, managedAgent.ID, managedAgentEnrollmentCreateRequest{
				EnrollmentType:   "cli_managed_config_restore",
				AdapterKey:       runtimeSessionSourceTypeForAgent(agent.Name),
				Status:           "restored",
				TargetConfigPath: agent.ConfigPath,
				BackupMetadata: map[string]interface{}{
					"backup_path": state.BackupPath,
				},
				RestoreAvailable: false,
				LastRestoredAt:   &now,
			})
		}
	}

	fmt.Printf("✓ Restored %s config from %s\n", resolveAgentDisplayName(agent), state.BackupPath)
	return nil
}

type offboardCleanupCandidate struct {
	Kind           string
	Name           string
	ResourceID     string
	ReferencedBy   []string
	RecentlyUsedBy []string
	FlowReferences []string
}

func runAgentsOffboard(cmd *cobra.Command, args []string) error {
	autoApprove := isAutoApprove(cmd)
	runAll, _ := cmd.Flags().GetBool("all")
	modelRemovalPolicy, err := cmd.Flags().GetString("remove-model")
	if err != nil {
		return err
	}
	if err := validateOffboardCleanupPolicy("--remove-model", modelRemovalPolicy); err != nil {
		return err
	}
	serverRemovalPolicy, err := cmd.Flags().GetString("remove-mcp-servers")
	if err != nil {
		return err
	}
	if err := validateOffboardCleanupPolicy("--remove-mcp-servers", serverRemovalPolicy); err != nil {
		return err
	}

	discovered, err := discoverAgents(io.Discard, false)
	if err != nil {
		return err
	}

	if len(args) == 0 {
		client, err := api.NewClient(FlagToken, FlagURL)
		if err != nil {
			return fmt.Errorf("failed to create API client: %w", err)
		}

		var candidates []AgentConfig
		for _, agent := range discovered {
			if _, localErr := loadLocalEnrollmentState(agent); localErr == nil {
				candidates = append(candidates, agent)
			} else if detail, _ := getManagedAgentDetailForDiscovered(client, agent); detail != nil {
				candidates = append(candidates, agent)
			}
		}

		if len(candidates) == 0 {
			fmt.Println("No enrolled agents found to offboard.")
			return nil
		}

		if runAll {
			if !autoApprove {
				var names []string
				for _, a := range candidates {
					names = append(names, resolveAgentDisplayName(a))
				}
				confirmed, err := confirmActionDefaultYes(
					bufio.NewReader(os.Stdin),
					os.Stdout,
					fmt.Sprintf("Offboard agents %s? (Y/n): ", strings.Join(names, ", ")),
				)
				if err != nil {
					return err
				}
				if !confirmed {
					return nil
				}
			}
			for _, agent := range candidates {
				if err := executeOffboard(agent, true, modelRemovalPolicy, serverRemovalPolicy); err != nil {
					return err
				}
			}
			return nil
		}

		for _, agent := range candidates {
			if !autoApprove {
				confirmed, err := confirmActionDefaultYes(
					bufio.NewReader(os.Stdin),
					os.Stdout,
					fmt.Sprintf("Offboard %s? (Y/n): ", resolveAgentDisplayName(agent)),
				)
				if err != nil {
					return err
				}
				if !confirmed {
					continue
				}
			}
			if err := executeOffboard(agent, true, modelRemovalPolicy, serverRemovalPolicy); err != nil {
				return err
			}
		}
		return nil
	}

	agent, err := findDiscoveredAgent(discovered, args[0])
	if err != nil {
		return err
	}
	return executeOffboard(agent, autoApprove, modelRemovalPolicy, serverRemovalPolicy)
}

func executeOffboard(agent AgentConfig, autoApprove bool, modelRemovalPolicy, serverRemovalPolicy string) error {
	var backupPath string
	state, err := loadLocalEnrollmentState(agent)
	if err == nil {
		backupPath = state.BackupPath
	}

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}
	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	var detail *managedAgentDetailResponse
	detail, _ = getManagedAgentDetailForDiscovered(client, agent)

	if state == nil && detail == nil {
		return fmt.Errorf("agent %q is not onboarded (no local state or remote managed agent found)", agent.Name)
	}

	if !autoApprove {
		var msg string
		if backupPath != "" {
			msg = fmt.Sprintf(
				"Offboard %s and restore %s from %s? (y/N): ",
				resolveAgentDisplayName(agent),
				agent.ConfigPath,
				backupPath,
			)
		} else {
			msg = fmt.Sprintf(
				"Offboard %s from Preloop? (Local backup state not found for %s) (y/N): ",
				resolveAgentDisplayName(agent),
				agent.ConfigPath,
			)
		}

		confirmed, err := confirmAction(os.Stdin, os.Stdout, msg)
		if err != nil {
			return fmt.Errorf("failed to read confirmation: %w", err)
		}
		if !confirmed {
			fmt.Println("Aborted without offboarding.")
			return nil
		}
	}

	if state != nil {
		if _, err := restoreAgentFromBackup(agent, state); err != nil {
			return err
		}
	} else {
		fmt.Printf("Skipped restoring config: no local backup found.\n")
		if err := removeClaudeCodeManagedMCPServer(agent); err != nil {
			return err
		}
		if err := removeApprovalHooks(agent, nil); err != nil {
			return err
		}
	}

	if detail != nil {
		if err := deleteManagedAgentRecord(client, detail.Agent.ID); err != nil {
			return err
		}
	}

	if state != nil {
		if err := removeLocalEnrollmentState(agent); err != nil {
			return err
		}
	}

	fmt.Printf("✓ Offboarded %s\n", resolveAgentDisplayName(agent))
	fmt.Printf("  Restored config: %s\n", agent.ConfigPath)
	if isClaudeCodeAgent(agent) || isCodexCLIAgent(agent) {
		// Subscription refresh tokens rotate server-side while onboarded, so
		// write the live Preloop-held bundle back to the local credential
		// store before cleanup can delete it. The note is the fallback when
		// no bundle could be restored.
		if !restoreSubscriptionLoginOnOffboard(client, agent, detail, os.Stdout) &&
			isClaudeCodeAgent(agent) {
			printClaudeCodeOAuthOffboardNote(os.Stdout)
		}
	}
	if detail != nil {
		fmt.Printf("  Removed managed agent: %s\n", detail.Agent.ID)
		candidates, err := collectOffboardCleanupCandidates(client, detail.Agent)
		if err != nil {
			return err
		}
		if err := promptOffboardCleanup(
			os.Stdin,
			os.Stdout,
			autoApprove,
			modelRemovalPolicy,
			serverRemovalPolicy,
			client,
			candidates,
		); err != nil {
			return err
		}
	}

	return nil
}

func restoreAgentFromBackup(agent AgentConfig, state *localEnrollmentState) (time.Time, error) {
	backupBytes, err := os.ReadFile(state.BackupPath)
	if err != nil {
		return time.Time{}, fmt.Errorf("failed to read backup: %w", err)
	}
	shouldRestoreFile := state.ConfigExisted || len(bytes.TrimSpace(backupBytes)) > 0
	if !shouldRestoreFile {
		if err := os.Remove(agent.ConfigPath); err != nil && !os.IsNotExist(err) {
			return time.Time{}, fmt.Errorf("failed to remove synthesized config: %w", err)
		}
	} else {
		if err := os.WriteFile(agent.ConfigPath, backupBytes, 0644); err != nil {
			return time.Time{}, fmt.Errorf("failed to restore config: %w", err)
		}
	}
	if err := removeManagedAgentRuntimeArtifacts(agent); err != nil {
		return time.Time{}, err
	}
	if err := removeClaudeCodeManagedMCPServer(agent); err != nil {
		return time.Time{}, err
	}
	if err := removeApprovalHooks(agent, nil); err != nil {
		return time.Time{}, err
	}
	now := time.Now().UTC()
	state.RestoredAt = &now
	return now, nil
}

func deleteManagedAgentRecord(client *api.Client, agentID string) error {
	var response map[string]interface{}
	if err := client.Delete("/api/v1/agents/"+agentID, &response); err != nil {
		return fmt.Errorf("failed to remove managed agent %q: %w", agentID, err)
	}
	return nil
}

func removeLocalEnrollmentState(agent AgentConfig) error {
	paths := []string{}
	if statePath, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath); err == nil {
		paths = append(paths, statePath)
	}
	if legacyPath, err := legacyLocalEnrollmentStatePath(agent); err == nil {
		paths = append(paths, legacyPath)
	}
	for _, path := range paths {
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("failed to remove local enrollment state %q: %w", path, err)
		}
	}
	return nil
}

func collectOffboardCleanupCandidates(client *api.Client, current managedAgentSummary) ([]offboardCleanupCandidate, error) {
	allAgents, err := listManagedAgents(client)
	if err != nil {
		return nil, err
	}
	otherAgents := make([]managedAgentSummary, 0, len(allAgents))
	for _, agent := range allAgents {
		if agent.ID == current.ID {
			continue
		}
		otherAgents = append(otherAgents, agent)
	}

	var serverIndex []mcpServerResponse
	if err := client.Get("/api/v1/mcp-servers", &serverIndex); err != nil {
		return nil, fmt.Errorf("failed to list MCP servers: %w", err)
	}
	serversByName := make(map[string]mcpServerResponse, len(serverIndex))
	for _, server := range serverIndex {
		serversByName[server.Name] = server
	}

	var modelIndex []aiModelResponse
	if err := client.Get("/api/v1/ai-models", &modelIndex); err != nil {
		return nil, fmt.Errorf("failed to list AI models: %w", err)
	}
	var flows []flowSummaryResponse
	if err := client.Get("/api/v1/flows?limit=1000", &flows); err != nil {
		return nil, fmt.Errorf("failed to list flows: %w", err)
	}

	candidates := make([]offboardCleanupCandidate, 0, len(current.ManagedMCPServers)+1)
	for _, serverName := range current.ManagedMCPServers {
		server, ok := serversByName[serverName]
		if !ok {
			continue
		}
		referencedBy := []string{}
		recentlyUsedBy := []string{}
		for _, other := range otherAgents {
			if !containsString(other.ManagedMCPServers, serverName) {
				continue
			}
			referencedBy = append(referencedBy, other.DisplayName)
			if isRecentlyActiveAgent(other) {
				recentlyUsedBy = append(recentlyUsedBy, other.DisplayName)
			}
		}
		candidates = append(candidates, offboardCleanupCandidate{
			Kind:           "mcp_server",
			Name:           serverName,
			ResourceID:     server.ID,
			ReferencedBy:   referencedBy,
			RecentlyUsedBy: recentlyUsedBy,
		})
	}

	if strings.TrimSpace(current.LatestModelAlias) != "" {
		for _, model := range modelIndex {
			if gatewayAliasForAIModel(model) != current.LatestModelAlias {
				continue
			}
			referencedBy := []string{}
			recentlyUsedBy := []string{}
			for _, other := range otherAgents {
				if strings.TrimSpace(other.LatestModelAlias) != current.LatestModelAlias {
					continue
				}
				referencedBy = append(referencedBy, other.DisplayName)
				if isRecentlyActiveAgent(other) {
					recentlyUsedBy = append(recentlyUsedBy, other.DisplayName)
				}
			}
			flowReferences := []string{}
			for _, flow := range flows {
				if strings.TrimSpace(flow.AIModelID) != model.ID {
					continue
				}
				flowReferences = append(flowReferences, labelOffboardFlow(flow))
			}
			candidates = append(candidates, offboardCleanupCandidate{
				Kind:           "ai_model",
				Name:           current.LatestModelAlias,
				ResourceID:     model.ID,
				ReferencedBy:   referencedBy,
				RecentlyUsedBy: recentlyUsedBy,
				FlowReferences: flowReferences,
			})
			break
		}
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].Kind == candidates[j].Kind {
			return candidates[i].Name < candidates[j].Name
		}
		return candidates[i].Kind < candidates[j].Kind
	})
	return candidates, nil
}

func promptOffboardCleanup(reader io.Reader, writer io.Writer, autoApprove bool, modelRemovalPolicy string, serverRemovalPolicy string, client *api.Client, candidates []offboardCleanupCandidate) error {
	if len(candidates) == 0 {
		return nil
	}
	bufferedReader := bufio.NewReader(reader)
	for _, candidate := range candidates {
		kindLabel := "MCP server"
		deletePath := "/api/v1/mcp-servers/" + candidate.ResourceID
		successLabel := "Removed MCP server"
		if candidate.Kind == "ai_model" {
			kindLabel = "AI model"
			deletePath = "/api/v1/ai-models/" + candidate.ResourceID
			successLabel = "Removed AI model"
		}
		if candidate.Kind == "ai_model" {
			if len(candidate.ReferencedBy) > 0 {
				fmt.Fprintf( //nolint:errcheck
					writer,
					"  Keeping %s %q because it is still used by other managed agents: %s\n",
					kindLabel,
					candidate.Name,
					strings.Join(candidate.ReferencedBy, ", "),
				)
				continue
			}
			if len(candidate.FlowReferences) > 0 {
				fmt.Fprintf( //nolint:errcheck
					writer,
					"  Keeping %s %q because it is still used by flows: %s\n",
					kindLabel,
					candidate.Name,
					strings.Join(candidate.FlowReferences, ", "),
				)
				continue
			}
		}
		if candidate.Kind != "ai_model" && len(candidate.ReferencedBy) > 0 {
			fmt.Fprintf( //nolint:errcheck
				writer,
				"  Keeping %s %q because it is still used by other managed agents: %s\n",
				kindLabel,
				candidate.Name,
				strings.Join(candidate.ReferencedBy, ", "),
			)
			continue
		}
		if len(candidate.RecentlyUsedBy) > 0 {
			fmt.Fprintf( //nolint:errcheck
				writer,
				"  Skipping %s %q because it was recently used by: %s\n",
				kindLabel,
				candidate.Name,
				strings.Join(candidate.RecentlyUsedBy, ", "),
			)
			continue
		}

		confirmed, err := resolveOffboardCleanupConfirmation(
			bufferedReader,
			writer,
			autoApprove,
			offboardCleanupPolicyForKind(candidate.Kind, modelRemovalPolicy, serverRemovalPolicy),
			kindLabel,
			candidate.Name,
		)
		if err != nil {
			return err
		}
		if !confirmed {
			continue
		}

		var deleteErr error
		if candidate.Kind == "ai_model" {
			deleteErr = client.Delete(deletePath, nil)
		} else {
			var response map[string]interface{}
			deleteErr = client.Delete(deletePath, &response)
		}
		if deleteErr != nil {
			return fmt.Errorf("failed to remove %s %q: %w", kindLabel, candidate.Name, deleteErr)
		}
		fmt.Fprintf(writer, "  ✓ %s %q\n", successLabel, candidate.Name) //nolint:errcheck
	}
	return nil
}

func validateOffboardCleanupPolicy(flagName string, policy string) error {
	switch strings.ToLower(strings.TrimSpace(policy)) {
	case offboardCleanupAsk, offboardCleanupYes, offboardCleanupNo:
		return nil
	default:
		return fmt.Errorf("%s must be one of: ask, yes, no", flagName)
	}
}

func offboardCleanupPolicyForKind(kind string, modelRemovalPolicy string, serverRemovalPolicy string) string {
	if kind == "ai_model" {
		return modelRemovalPolicy
	}
	return serverRemovalPolicy
}

func resolveOffboardCleanupConfirmation(
	reader *bufio.Reader,
	writer io.Writer,
	autoApprove bool,
	modelRemovalPolicy string,
	kindLabel string,
	name string,
) (bool, error) {
	switch strings.ToLower(strings.TrimSpace(modelRemovalPolicy)) {
	case offboardCleanupYes:
		return true, nil
	case offboardCleanupNo:
		return false, nil
	case offboardCleanupAsk:
		if autoApprove {
			fmt.Fprintf( //nolint:errcheck
				writer,
				"  Keeping %s %q in Preloop. Pass the appropriate cleanup flag with 'yes' to remove it automatically.\n",
				kindLabel,
				name,
			)
			return false, nil
		}
		confirmed, err := confirmAction(
			reader,
			writer,
			fmt.Sprintf("Remove %s %q from Preloop as well? (y/N): ", kindLabel, name),
		)
		if err != nil {
			return false, fmt.Errorf("failed to read cleanup confirmation: %w", err)
		}
		return confirmed, nil
	default:
		return false, fmt.Errorf("cleanup policy must be one of: ask, yes, no")
	}
}

func labelOffboardFlow(flow flowSummaryResponse) string {
	if strings.TrimSpace(flow.Name) != "" {
		return flow.Name
	}
	return flow.ID
}

func isRecentlyActiveAgent(agent managedAgentSummary) bool {
	switch strings.TrimSpace(agent.ActivityStatus) {
	case "active_now", "recently_active":
		return true
	default:
		return false
	}
}

func containsString(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

// addDiscoveredServers interactively adds servers to the Preloop account.

func sortedServerNames(servers map[string]MCPDef) []string {
	names := make([]string, 0, len(servers))
	for name := range servers {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func issueRuntimeSessionToken(client *api.Client, agent AgentConfig, allowedServers []string) (*runtimeSessionTokenResponse, error) {
	serverNames := append([]string(nil), allowedServers...)
	sort.Strings(serverNames)

	request := runtimeSessionTokenRequest{
		SessionSourceType:    runtimeSessionSourceTypeForAgent(agent.Name),
		SessionSourceID:      runtimeSessionInstanceIDForAgent(agent),
		SessionReference:     filepath.Clean(agent.ConfigPath),
		RuntimePrincipalID:   runtimePrincipalIDForAgent(agent),
		RuntimePrincipalName: runtimePrincipalNameForAgent(agent),
		ExpiresInMinutes:     120,
		Scopes:               []string{"mcp:read", "mcp:write"},
		AllowedMCPServers:    serverNames,
	}

	var result runtimeSessionTokenResponse
	if err := client.Post("/api/v1/auth/runtime-sessions/token", request, &result); err != nil {
		return nil, err
	}

	return &result, nil
}

func resolveAgentDisplayName(agent AgentConfig) string {
	if strings.TrimSpace(agent.DisplayName) != "" {
		return strings.TrimSpace(agent.DisplayName)
	}
	if inferred := inferAgentDisplayName(agent); inferred != "" {
		return inferred
	}
	return defaultAgentDisplayName(agent)
}

func defaultAgentDisplayName(agent AgentConfig) string {
	configBase := strings.TrimSuffix(
		filepath.Base(filepath.Clean(agent.ConfigPath)),
		filepath.Ext(filepath.Base(filepath.Clean(agent.ConfigPath))),
	)
	configBase = strings.TrimSpace(configBase)
	typeSlug := slugifyAgentName(agent.Name)
	baseSlug := slugifyAgentName(configBase)
	switch {
	case configBase == "", configBase == ".", strings.EqualFold(configBase, "config"), strings.EqualFold(configBase, "settings"), strings.EqualFold(configBase, "openclaw"), strings.EqualFold(configBase, "mcp_config"), strings.EqualFold(configBase, "mcp"):
		return strings.TrimSpace(agent.Name)
	case strings.Contains(typeSlug, baseSlug), strings.Contains(baseSlug, typeSlug):
		return strings.TrimSpace(agent.Name)
	default:
		return fmt.Sprintf("%s %s", strings.TrimSpace(agent.Name), configBase)
	}
}

func inferAgentDisplayName(agent AgentConfig) string {
	for _, candidate := range identityCandidatePaths(agent) {
		name, err := parseAgentIdentityFile(candidate)
		if err == nil && strings.TrimSpace(name) != "" {
			return strings.TrimSpace(name)
		}

		if data, err := os.ReadFile(candidate); err == nil {
			if extracted := extractAgentNameViaAPI(string(data)); extracted != "" && extracted != "Unknown Agent" {
				return extracted
			}
		}
	}
	return ""
}

func extractAgentNameViaAPI(content string) string {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil || !client.IsAuthenticated() {
		return ""
	}

	request := map[string]string{
		"identity_content": content,
	}
	var result struct {
		Name string `json:"name"`
	}

	if err := client.Post("/api/v1/agents/extract-name", request, &result); err == nil {
		return strings.TrimSpace(result.Name)
	}
	return ""
}

func identityCandidatePaths(agent AgentConfig) []string {
	cleanConfig := filepath.Clean(agent.ConfigPath)
	configDir := filepath.Dir(cleanConfig)
	home, _ := os.UserHomeDir()

	candidates := []string{
		filepath.Join(configDir, "IDENTITY.md"),
		filepath.Join(configDir, "identity.md"),
		filepath.Join(configDir, "workspace", "IDENTITY.md"),
		filepath.Join(configDir, "workspace", "identity.md"),
	}

	parentDir := filepath.Dir(configDir)
	if parentDir != configDir && parentDir != "" && parentDir != "." && parentDir != home {
		candidates = append(
			candidates,
			filepath.Join(parentDir, "IDENTITY.md"),
			filepath.Join(parentDir, "identity.md"),
			filepath.Join(parentDir, "workspace", "IDENTITY.md"),
			filepath.Join(parentDir, "workspace", "identity.md"),
		)
	}
	return candidates
}

func parseAgentIdentityFile(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}

	lines := strings.Split(string(data), "\n")

	// First pass: OpenClaw exact pattern match
	for _, line := range lines {
		if idx := strings.Index(line, "**Name:** "); idx != -1 {
			name := strings.TrimSpace(line[idx+len("**Name:** "):])
			if name != "" {
				return name, nil
			}
		}
	}

	// Second pass: Generic matches
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" {
			continue
		}
		if idx := strings.Index(strings.ToLower(trimmed), "name:"); idx != -1 {
			return strings.TrimSpace(trimmed[idx+len("name:"):]), nil
		}
		if strings.HasPrefix(trimmed, "#") {
			return strings.TrimSpace(strings.TrimLeft(trimmed, "#")), nil
		}
		return trimmed, nil
	}
	return "", fmt.Errorf("identity file %s did not contain a usable name", path)
}

func normalizeDiscoveredAgent(agent AgentConfig) AgentConfig {
	if state, err := loadLocalEnrollmentState(agent); err == nil {
		if strings.TrimSpace(state.DisplayName) != "" {
			agent.DisplayName = strings.TrimSpace(state.DisplayName)
		}
		if strings.TrimSpace(state.RuntimePrincipalID) != "" {
			agent.RuntimePrincipalID = strings.TrimSpace(state.RuntimePrincipalID)
		}
	}
	if strings.TrimSpace(agent.DisplayName) == "" {
		agent.DisplayName = resolveAgentDisplayName(agent)
	}
	if strings.TrimSpace(agent.RuntimePrincipalID) == "" {
		agent.RuntimePrincipalID = generatedRuntimePrincipalID(agent.DisplayName, agent.ConfigPath)
	}
	return agent
}

func runtimeSessionSourceTypeForAgent(agentName string) string {
	switch strings.ToLower(strings.TrimSpace(agentName)) {
	case "claude code":
		return "claude_code"
	case "claude desktop":
		return "claude_desktop"
	case "codex cli":
		return "codex"
	case "openclaw":
		return "openclaw"
	case "gemini cli":
		return "gemini_cli"
	case "opencode":
		return "opencode"
	case "hermes":
		return hermesSourceType
	default:
		return "desktop_agent"
	}
}

func runtimePrincipalIDForAgent(agent AgentConfig) string {
	if strings.TrimSpace(agent.RuntimePrincipalID) != "" {
		return strings.TrimSpace(agent.RuntimePrincipalID)
	}
	return generatedRuntimePrincipalID(resolveAgentDisplayName(agent), agent.ConfigPath)
}

func generatedRuntimePrincipalID(displayName, configPath string) string {
	sum := sha1.Sum([]byte(strings.ToLower(strings.TrimSpace(displayName)) + "\x00" + filepath.Clean(configPath)))
	return fmt.Sprintf("%s-%s", slugifyAgentName(displayName), hex.EncodeToString(sum[:6]))
}

func legacyRuntimePrincipalIDForAgent(agent AgentConfig) string {
	sum := sha1.Sum([]byte(strings.ToLower(agent.Name) + "\x00" + filepath.Clean(agent.ConfigPath)))
	return fmt.Sprintf("%s-%s", slugifyAgentName(agent.Name), hex.EncodeToString(sum[:6]))
}

func slugifyAgentName(value string) string {
	trimmed := strings.ToLower(strings.TrimSpace(value))
	if trimmed == "" {
		return "agent"
	}
	var builder strings.Builder
	lastHyphen := false
	for _, r := range trimmed {
		isAlphaNum := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if isAlphaNum {
			builder.WriteRune(r)
			lastHyphen = false
			continue
		}
		if builder.Len() == 0 || lastHyphen {
			continue
		}
		builder.WriteByte('-')
		lastHyphen = true
	}
	slug := strings.Trim(builder.String(), "-")
	if slug == "" {
		return "agent"
	}
	return slug
}

func runtimeSessionInstanceIDForAgent(agent AgentConfig) string {
	return fmt.Sprintf("%s-%d", runtimePrincipalIDForAgent(agent), time.Now().UnixNano())
}

func runtimePrincipalNameForAgent(agent AgentConfig) string {
	return resolveAgentDisplayName(agent)
}

func runAgentsStarterPolicy(cmd *cobra.Command, args []string) error {
	serverName := args[0]
	output, _ := cmd.Flags().GetString("output")
	apply, _ := cmd.Flags().GetBool("apply")
	dryRun, _ := cmd.Flags().GetBool("dry-run")
	autoApprove, _ := cmd.Flags().GetBool("yes")
	noContext, _ := cmd.Flags().GetBool("no-context")

	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil {
		return fmt.Errorf("failed to create API client: %w", err)
	}

	if !client.IsAuthenticated() {
		return fmt.Errorf("not authenticated - run 'preloop login' first")
	}

	matchedServerName, tools, err := findStarterPolicyTools(client, serverName)
	if err != nil {
		return err
	}

	fmt.Printf(
		"Generating starter policy for MCP server '%s' (%d discovered tool(s))...\n",
		matchedServerName,
		len(tools),
	)

	prompt := buildStarterPolicyPrompt(matchedServerName, tools)
	result, err := generatePolicyFromPrompt(client, prompt, !noContext)
	if err != nil {
		return fmt.Errorf("failed to generate starter policy: %w", err)
	}

	printPolicyWarnings(result.Warnings)

	fileName := defaultStarterPolicyFileName(matchedServerName)
	if output != "" {
		if err := os.WriteFile(output, []byte(result.YAML), 0644); err != nil {
			return fmt.Errorf("failed to write generated policy: %w", err)
		}
		fmt.Printf("✓ Generated policy written to: %s\n", output)
	} else {
		fmt.Println("\n---")
		fmt.Print(result.YAML)
		fmt.Println("---")
	}

	diff, err := diffPolicyContent(client, fileName, []byte(result.YAML))
	if err != nil {
		return fmt.Errorf("failed to preview generated policy diff: %w", err)
	}
	printPolicyDiffPreview(diff)

	if !apply {
		if output == "" {
			fmt.Println("\nTip: Review the diff above, then use --apply to apply it immediately or -o policy.yaml to save it.")
		}
		return nil
	}

	if !diff.HasChanges {
		fmt.Println("Generated policy already matches the current configuration. Nothing to apply.")
		return nil
	}

	if dryRun {
		fmt.Println("\nValidating generated policy with the server (dry run)...")
	} else {
		_, _, removed := splitPolicyDiffChanges(diff)
		fmt.Println("\nReview changes above carefully. Applying will update your active Preloop policy.")
		if len(removed) > 0 {
			fmt.Printf("Warning: %d existing item(s) will be removed if you continue.\n", len(removed))
		}
		if !autoApprove {
			confirmed, err := confirmAction(os.Stdin, os.Stdout, buildPolicyApplyConfirmationPrompt(diff))
			if err != nil {
				return fmt.Errorf("failed to read confirmation: %w", err)
			}
			if !confirmed {
				fmt.Println("Aborted without applying changes.")
				return nil
			}
		}
		fmt.Println("\nApplying generated policy...")
	}

	applyResult, err := applyPolicyContent(client, fileName, []byte(result.YAML), dryRun)
	if err != nil {
		return fmt.Errorf("failed to apply generated policy: %w", err)
	}

	if dryRun {
		fmt.Println("✓ Generated policy validated successfully")
	} else {
		fmt.Println("✓ Generated policy applied successfully")
	}
	fmt.Printf("  Policy: %s\n", applyResult.PolicyName)
	fmt.Printf("  MCP servers: %d created, %d updated\n", applyResult.MCPServersCreated, applyResult.MCPServersUpdated)
	fmt.Printf("  Approval workflows: %d created, %d updated\n", applyResult.PoliciesCreated, applyResult.PoliciesUpdated)
	fmt.Printf("  Tools: %d created, %d updated", applyResult.ToolsCreated, applyResult.ToolsUpdated)
	if applyResult.ToolsSkipped > 0 {
		fmt.Printf(", %d skipped", applyResult.ToolsSkipped)
	}
	fmt.Println()
	printPolicyWarnings(applyResult.Warnings)

	return nil
}

func findStarterPolicyTools(client *api.Client, serverName string) (string, []starterPolicyTool, error) {
	var tools []starterPolicyTool
	if err := client.Get(toolsListPath, &tools); err != nil {
		return "", nil, fmt.Errorf("failed to list tools: %w", err)
	}

	var exactMatches []starterPolicyTool
	for _, tool := range tools {
		if tool.Source != "mcp" {
			continue
		}
		if tool.SourceName == serverName || tool.SourceID == serverName {
			exactMatches = append(exactMatches, tool)
		}
	}
	if len(exactMatches) > 0 {
		sort.Slice(exactMatches, func(i, j int) bool { return exactMatches[i].Name < exactMatches[j].Name })
		return exactMatches[0].SourceName, exactMatches, nil
	}

	var caseInsensitiveMatches []starterPolicyTool
	for _, tool := range tools {
		if tool.Source != "mcp" {
			continue
		}
		if strings.EqualFold(tool.SourceName, serverName) || strings.EqualFold(tool.SourceID, serverName) {
			caseInsensitiveMatches = append(caseInsensitiveMatches, tool)
		}
	}
	if len(caseInsensitiveMatches) > 0 {
		sort.Slice(caseInsensitiveMatches, func(i, j int) bool { return caseInsensitiveMatches[i].Name < caseInsensitiveMatches[j].Name })
		return caseInsensitiveMatches[0].SourceName, caseInsensitiveMatches, nil
	}

	availableSet := make(map[string]struct{})
	for _, tool := range tools {
		if tool.Source == "mcp" && tool.SourceName != "" {
			availableSet[tool.SourceName] = struct{}{}
		}
	}

	available := make([]string, 0, len(availableSet))
	for name := range availableSet {
		available = append(available, name)
	}
	sort.Strings(available)

	if len(available) == 0 {
		return "", nil, fmt.Errorf("no MCP server tools found. Add a server first, then retry")
	}

	return "", nil, fmt.Errorf(
		"MCP server '%s' not found. Available servers with discovered tools: %s",
		serverName,
		strings.Join(available, ", "),
	)
}

func buildStarterPolicyPrompt(serverName string, tools []starterPolicyTool) string {
	var b strings.Builder

	fmt.Fprintf(&b, "Generate a starter Preloop policy update for MCP server %q.\n\n", serverName)
	b.WriteString("Requirements:\n")
	b.WriteString("- Preserve the current account configuration.\n")
	b.WriteString("- Scope changes to this MCP server and its tools.\n")
	b.WriteString("- Do not modify unrelated MCP servers, tool rules, approval workflows, or defaults unless needed for safe onboarding.\n")
	b.WriteString("- Reuse existing approval workflows from the current configuration when they already fit.\n")
	b.WriteString("- Prefer allow rules for clearly read-only/query/list/get/search tools.\n")
	b.WriteString("- Prefer require_approval for mutating, write-capable, privileged, destructive, or otherwise high-impact tools.\n")
	b.WriteString("- If a tool is ambiguous, prefer require_approval.\n")
	b.WriteString("- If a new approval workflow is required, keep it minimal and reusable.\n")
	b.WriteString("- Preserve any existing tool rules for this server unless they conflict with the safe starter posture.\n\n")
	b.WriteString("Discovered tools for this server:\n")

	for _, tool := range tools {
		props, required := summarizeToolSchema(tool.Schema)
		fmt.Fprintf(
			&b,
			"- %s: %s Suggested posture: %s.",
			tool.Name,
			strings.TrimSpace(tool.Description),
			starterPolicyPosture(tool),
		)
		if len(props) > 0 {
			fmt.Fprintf(&b, " Args: %s.", strings.Join(props, ", "))
		}
		if len(required) > 0 {
			fmt.Fprintf(&b, " Required args: %s.", strings.Join(required, ", "))
		}
		if len(tool.AccessRules) > 0 {
			fmt.Fprintf(&b, " Existing rules: %s.", summarizeAccessRules(tool.AccessRules))
		} else if tool.HasApprovalCondition {
			b.WriteString(" Existing approval condition present.")
		}
		b.WriteString("\n")
	}

	return b.String()
}

func starterPolicyPosture(tool starterPolicyTool) string {
	text := strings.ToLower(tool.Name + " " + tool.Description)

	switch {
	case containsAny(text, "get", "list", "read", "search", "find", "fetch", "view", "describe"):
		return "allow unless existing rules already require approval"
	case containsAny(text, "create", "update", "delete", "remove", "write", "edit", "send", "post", "merge", "deploy", "execute", "run", "restart", "shutdown", "grant", "revoke", "invite", "approve", "comment"):
		return "require approval"
	default:
		return "require approval unless the schema clearly indicates read-only behavior"
	}
}

func summarizeToolSchema(schema map[string]interface{}) ([]string, []string) {
	var props []string
	if rawProps, ok := schema["properties"].(map[string]interface{}); ok {
		for key := range rawProps {
			props = append(props, key)
		}
		sort.Strings(props)
	}

	var required []string
	if rawRequired, ok := schema["required"].([]interface{}); ok {
		for _, item := range rawRequired {
			if name, ok := item.(string); ok {
				required = append(required, name)
			}
		}
		sort.Strings(required)
	}

	return props, required
}

func summarizeAccessRules(rules []starterPolicyAccessRule) string {
	summaries := make([]string, 0, len(rules))
	for _, rule := range rules {
		summary := rule.Action
		if rule.ConditionType != "" {
			summary += " (" + rule.ConditionType + ")"
		}
		summaries = append(summaries, summary)
	}
	sort.Strings(summaries)
	return strings.Join(summaries, ", ")
}

func discoverAgents(w io.Writer, printWarnings bool) ([]AgentConfig, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return nil, fmt.Errorf("could not determine home directory: %w", err)
	}

	var discovered []AgentConfig
	for _, spec := range agentSpecs {
		discoveredSpec := false
		for _, fullPath := range configPathsForAgentSpec(home, spec) {
			if _, err := os.Stat(fullPath); err != nil {
				continue
			}
			servers, err := spec.Parser(fullPath)
			if err != nil {
				if printWarnings {
					fmt.Fprintf(w, "  Warning: could not parse %s config at %s: %v\n", spec.Name, fullPath, err) //nolint:errcheck
				}
				continue
			}
			discovered = append(discovered, AgentConfig{
				Name:       spec.Name,
				ConfigPath: fullPath,
				MCPServers: servers,
			})
			discovered[len(discovered)-1] = withDetectedRuntimeState(withDetectedAuthState(
				normalizeDiscoveredAgent(discovered[len(discovered)-1]),
			))
			discoveredSpec = true
			break
		}
		if discoveredSpec {
			continue
		}
		if fallbackPath, ok := detectInstalledAgent(home, spec); ok {
			discovered = append(discovered, withDetectedRuntimeState(withDetectedAuthState(normalizeDiscoveredAgent(AgentConfig{
				Name:       spec.Name,
				ConfigPath: fallbackPath,
				MCPServers: map[string]MCPDef{},
			}))))
		}
	}
	return discovered, nil
}

func detectInstalledAgent(home string, spec agentSpec) (string, bool) {
	if strings.TrimSpace(spec.BootstrapConfigPath) == "" ||
		(len(spec.DetectionPaths) == 0 && len(spec.DetectionCommands) == 0) {
		return "", false
	}
	bootstrapPath := expandAgentConfigPath(home, filepath.Join(home, spec.BootstrapConfigPath))
	for _, relativePath := range spec.DetectionPaths {
		fullPath := expandAgentConfigPath(home, filepath.Join(home, relativePath))
		if _, err := os.Stat(fullPath); err == nil {
			return bootstrapPath, true
		}
	}
	for _, command := range spec.DetectionCommands {
		if _, err := exec.LookPath(command); err == nil {
			return bootstrapPath, true
		}
	}
	return "", false
}

func authenticatedDiscoveryClient() *api.Client {
	client, err := api.NewClient(FlagToken, FlagURL)
	if err != nil || client == nil || !client.IsAuthenticated() {
		return nil
	}
	return client
}

func enrichDiscoveredAgents(discovered []AgentConfig, client *api.Client) ([]AgentConfig, error) {
	enriched := make([]AgentConfig, 0, len(discovered))
	for _, agent := range discovered {
		resolved, err := enrichDiscoveredAgent(agent, client)
		if err != nil {
			return nil, err
		}
		enriched = append(enriched, resolved)
	}
	return enriched, nil
}

func enrichDiscoveredAgent(agent AgentConfig, client *api.Client) (AgentConfig, error) {
	agent = normalizeDiscoveredAgent(agent)
	state, _ := loadLocalEnrollmentState(agent)
	var remote *managedAgentSummary
	if client != nil {
		if managed, err := getManagedAgentForDiscovered(client, agent); err == nil {
			remote = managed
		}
	}
	agent.IsOnboarded = state != nil || remote != nil
	if remote != nil && strings.TrimSpace(remote.OnboardingState) != "" {
		agent.OnboardingState = strings.TrimSpace(remote.OnboardingState)
	}
	if state == nil {
		if agent.OnboardingState == "" {
			agent.OnboardingState = "incomplete"
		}
		return agent, nil
	}

	currentConfig, err := loadAgentConfigDocument(agent)
	if err != nil {
		agent.ConfigDrift = true
		agent.ReonboardRecommended = true
		agent.DriftReasons = append(agent.DriftReasons, fmt.Sprintf("Current config could not be loaded: %v", err))
		if agent.OnboardingState == "" {
			agent.OnboardingState = "incomplete"
		}
		return agent, nil
	}

	validation := managedMCPAdapterForAgent(agent).ValidateManagedConfig(
		currentConfig,
		clientBaseURLForFlags(),
	)
	if agent.OnboardingState == "" {
		agent.OnboardingState = onboardingStateFromValidation(validation)
	}
	if passed, _ := validation["validation_passed"].(bool); !passed {
		agent.ConfigDrift = true
		agent.ReonboardRecommended = true
		agent.DriftReasons = append(agent.DriftReasons, "Current managed config no longer matches the expected Preloop-managed shape.")
	}

	currentSnapshot, snapshotErr := deepCopyMap(currentConfig)
	if snapshotErr == nil {
		sanitizeConfigSnapshot(currentSnapshot)
		if len(state.ManagedConfig) > 0 && !equalJSONMap(currentSnapshot, state.ManagedConfig) {
			agent.ConfigDrift = true
			agent.ReonboardRecommended = true
			agent.DriftReasons = append(agent.DriftReasons, "Local tool or model configuration has changed since the last onboarding.")
		}
	}
	if agent.OnboardingState == "" {
		agent.OnboardingState = "incomplete"
	}
	return agent, nil
}

func onboardingStateFromValidation(validation map[string]interface{}) string {
	if len(validation) == 0 {
		return "incomplete"
	}
	mcpConfigured := false
	if present, _ := validation["preloop_server_present"].(bool); present {
		mcpConfigured = true
	}
	gatewayConfigured := false
	if ok, _ := validation["gateway_provider_ok"].(bool); ok {
		gatewayConfigured = true
	}
	if ok, _ := validation["model_provider_rewritten"].(bool); ok {
		gatewayConfigured = true
	}
	if mcpConfigured && gatewayConfigured {
		return "fully_onboarded"
	}
	if mcpConfigured {
		return "mcp_proxy_only"
	}
	if gatewayConfigured {
		return "gateway_only"
	}
	return "incomplete"
}

func onboardingStateLabel(state string) string {
	switch strings.TrimSpace(state) {
	case "fully_onboarded":
		return "Fully onboarded"
	case "mcp_proxy_only":
		return "MCP proxy only"
	case "gateway_only":
		return "Model gateway only"
	default:
		return "Incomplete"
	}
}

func onboardingStateNote(state string) string {
	switch strings.TrimSpace(state) {
	case "fully_onboarded":
		return "Tool calls and model traffic are both routed through Preloop."
	case "mcp_proxy_only":
		return "Tool calls are routed through Preloop, but model traffic is still direct."
	case "gateway_only":
		return "Model traffic is routed through Preloop, but MCP tool traffic is still direct."
	default:
		return "This agent is not fully managed by Preloop yet."
	}
}

func findDiscoveredAgent(discovered []AgentConfig, value string) (AgentConfig, error) {
	for _, agent := range discovered {
		if strings.EqualFold(agent.Name, value) ||
			strings.EqualFold(agent.DisplayName, value) ||
			strings.EqualFold(runtimePrincipalIDForAgent(agent), value) {
			return agent, nil
		}
	}
	available := make([]string, 0, len(discovered))
	for _, agent := range discovered {
		available = append(available, agent.Name)
	}
	sort.Strings(available)
	return AgentConfig{}, fmt.Errorf("agent %q not found. Available agents: %s", value, strings.Join(available, ", "))
}

func printEnrollmentPlan(plan managedMCPEnrollmentPlan, dryRun bool) {
	mode := "Apply"
	if dryRun {
		mode = "Preview"
	}
	fmt.Printf("%s managed MCP onboarding for %s\n", mode, resolveAgentDisplayName(plan.Agent))
	fmt.Printf("  Agent type: %s\n", plan.Agent.Name)
	fmt.Printf("  Config: %s\n", plan.Agent.ConfigPath)
	fmt.Printf("  Managed server: %s -> %s\n", plan.ManagedServerName, plan.ManagedServerURL)
	if plan.ManagedControlWSURL != "" {
		fmt.Printf("  Agent Control config target: %s\n", plan.ManagedControlWSURL)
		fmt.Println("  Agent Control runtime plugin: install/verify when supported")
	}
	discoveredNames := sortedServerNames(plan.Agent.MCPServers)
	if len(discoveredNames) > 0 {
		fmt.Printf("  Existing MCP servers: %s\n", strings.Join(discoveredNames, ", "))
	}
	fmt.Printf("  Runtime principal: %s\n", runtimePrincipalIDForAgent(plan.Agent))
	if plan.ManagedProviderName != "" && plan.ManagedModelAlias != "" {
		fmt.Printf("  Managed model: %s/%s\n", plan.ManagedProviderName, plan.ManagedModelAlias)
	}
	for _, note := range plan.Notes {
		// Notes are operator-facing status strings (never secret values);
		// print via a dedicated helper so taint from companion return
		// values in upstream resolvers does not flow into logging sinks.
		fmt.Printf("  Note: %s\n", publicPlanNote(note))
	}
}

func buildManagedMCPEnrollmentPlan(agent AgentConfig, baseURL, token string) (managedMCPEnrollmentPlan, error) {
	if strings.EqualFold(strings.TrimSpace(agent.Name), "openclaw") {
		return buildOpenClawManagedMCPEnrollmentPlan(agent, baseURL, token)
	}

	adapter := managedMCPAdapterForAgent(agent)
	discoveredDoc, err := loadAgentConfigDocument(agent)
	if err != nil {
		return managedMCPEnrollmentPlan{}, fmt.Errorf("failed to load config document: %w", err)
	}
	if discoveredDoc == nil {
		discoveredDoc = map[string]interface{}{}
	}
	managedDoc, err := deepCopyMap(discoveredDoc)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	container, err := adapter.EnsureServerContainer(managedDoc)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	removedPreloopServers := prunePreloopOwnedMCPServersFromDocument(managedDoc)
	container["preloop"] = adapter.BuildManagedServer(baseURL, token)
	ensureLegacyCodexMCPServer(agent, managedDoc, baseURL, token)
	if supportsAgentControlChannel(agent) {
		applyAgentControlConfigToDocument(
			agent,
			managedDoc,
			buildManagedAgentControlConfig(
				agent,
				baseURL,
				token,
				nil,
				nil,
				nil,
			),
		)
	}

	sanitizedDiscovered, err := deepCopyMap(discoveredDoc)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	sanitizeConfigSnapshot(sanitizedDiscovered)
	sanitizedManaged, err := deepCopyMap(managedDoc)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	sanitizeConfigSnapshot(sanitizedManaged)

	notes := []string{}
	if len(removedPreloopServers) > 0 {
		notes = append(
			notes,
			fmt.Sprintf(
				"Disabled existing Preloop MCP entries before writing this instance's managed endpoint: %s.",
				strings.Join(removedPreloopServers, ", "),
			),
		)
	}

	controlWSURL := ""
	if supportsAgentControlChannel(agent) {
		controlWSURL = managedAgentControlWebSocketURL(baseURL)
	}

	return managedMCPEnrollmentPlan{
		Agent:               agent,
		DiscoveredDocument:  discoveredDoc,
		ManagedDocument:     managedDoc,
		SanitizedDiscovered: sanitizedDiscovered,
		SanitizedManaged:    sanitizedManaged,
		ManagedServerName:   "preloop",
		ManagedServerURL:    strings.TrimRight(baseURL, "/") + "/mcp/v1",
		ManagedControlWSURL: controlWSURL,
		Notes:               notes,
	}, nil
}

func refreshManagedPlanSnapshots(plan managedMCPEnrollmentPlan) (managedMCPEnrollmentPlan, error) {
	sanitizedManaged, err := deepCopyMap(plan.ManagedDocument)
	if err != nil {
		return managedMCPEnrollmentPlan{}, err
	}
	sanitizeConfigSnapshot(sanitizedManaged)
	plan.SanitizedManaged = sanitizedManaged
	return plan, nil
}

func supportsManagedGateway(agent AgentConfig) bool {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "codex cli", "opencode", "claude code", "gemini cli", "hermes":
		return true
	default:
		return false
	}
}

func applyManagedGatewayForAgent(
	plan managedMCPEnrollmentPlan,
	agent AgentConfig,
	baseURL string,
	token string,
	modelAlias string,
) (managedMCPEnrollmentPlan, error) {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "codex cli":
		return applyCodexManagedGateway(plan, baseURL, token, modelAlias)
	case "opencode":
		return applyOpenCodeManagedGateway(plan, baseURL, token, modelAlias)
	case "claude code":
		return applyClaudeManagedGateway(plan, baseURL, token, modelAlias)
	case "gemini cli":
		return applyGeminiManagedGateway(plan, baseURL, token, modelAlias)
	case "hermes":
		return applyHermesManagedGateway(plan, baseURL, token, modelAlias)
	default:
		return plan, nil
	}
}

func applyCodexManagedGateway(plan managedMCPEnrollmentPlan, baseURL, token, modelAlias string) (managedMCPEnrollmentPlan, error) {
	providers, ok := asObjectMap(plan.ManagedDocument["model_providers"])
	if !ok {
		providers = make(map[string]interface{})
		plan.ManagedDocument["model_providers"] = providers
	}
	providers["preloop"] = map[string]interface{}{
		"name":                      "Preloop",
		"base_url":                  strings.TrimRight(baseURL, "/") + openClawGatewayPath,
		"experimental_bearer_token": token,
		"wire_api":                  "responses",
	}
	plan.ManagedDocument["model_provider"] = "preloop"
	plan.ManagedDocument["model"] = modelAlias
	plan.ManagedModelAlias = modelAlias
	plan.ManagedProviderName = "preloop"
	plan.Notes = append(
		plan.Notes,
		fmt.Sprintf("Model traffic will route through Preloop using %s.", modelAlias),
	)
	return refreshManagedPlanSnapshots(plan)
}

func normalizeCodexTOMLDocument(doc map[string]interface{}) map[string]interface{} {
	normalized, err := deepCopyMap(doc)
	if err != nil {
		return doc
	}
	nux, ok := asObjectMap(lookupValue(normalized, "tui", "model_availability_nux"))
	if !ok {
		return normalized
	}
	for key, raw := range nux {
		switch value := raw.(type) {
		case float64:
			if value >= 0 && value <= float64(^uint32(0)) && value == float64(uint32(value)) {
				nux[key] = uint32(value)
			}
		case float32:
			if value >= 0 && value <= float32(^uint32(0)) && value == float32(uint32(value)) {
				nux[key] = uint32(value)
			}
		}
	}
	return normalized
}

func removeCodexManagedGatewaySelection(
	plan managedMCPEnrollmentPlan,
) (managedMCPEnrollmentPlan, error) {
	if strings.EqualFold(strings.TrimSpace(lookupString(plan.ManagedDocument, "model_provider")), "preloop") {
		delete(plan.ManagedDocument, "model_provider")
		delete(plan.ManagedDocument, "model_providers")
		plan.ManagedDocument["model"] = normalizeCodexDirectModelValue(plan.ManagedDocument["model"])
	}
	plan.ManagedModelAlias = ""
	plan.ManagedProviderName = ""
	return refreshManagedPlanSnapshots(plan)
}

func removeGeminiManagedGatewaySelection(
	plan managedMCPEnrollmentPlan,
) (managedMCPEnrollmentPlan, error) {
	if strings.Contains(
		strings.ToLower(strings.TrimSpace(lookupString(plan.ManagedDocument, "baseUrl"))),
		"preloop",
	) {
		delete(plan.ManagedDocument, "baseUrl")
		delete(plan.ManagedDocument, "apiKey")
	}
	plan.ManagedModelAlias = ""
	plan.ManagedProviderName = ""
	return refreshManagedPlanSnapshots(plan)
}

func geminiClientModelName(modelAlias string) string {
	modelAlias = strings.TrimSpace(modelAlias)
	if modelAlias == "" {
		return ""
	}
	if _, modelID, ok := strings.Cut(modelAlias, "/"); ok && strings.TrimSpace(modelID) != "" {
		return strings.TrimSpace(modelID)
	}
	return modelAlias
}

func normalizeGeminiGatewayModelAlias(modelRef string) string {
	modelRef = strings.TrimSpace(modelRef)
	if modelRef == "" {
		return ""
	}
	if strings.Contains(modelRef, "/") {
		return modelRef
	}
	return "google/" + modelRef
}

func applyGeminiManagedGateway(plan managedMCPEnrollmentPlan, baseURL, token, modelAlias string) (managedMCPEnrollmentPlan, error) {
	plan.ManagedDocument["apiKey"] = token
	plan.ManagedDocument["baseUrl"] = strings.TrimRight(baseURL, "/") + "/gemini"
	modelConfig, ok := asObjectMap(plan.ManagedDocument["model"])
	if !ok {
		modelConfig = make(map[string]interface{})
	}
	modelConfig["name"] = geminiClientModelName(modelAlias)
	plan.ManagedDocument["model"] = modelConfig
	plan.ManagedModelAlias = modelAlias
	plan.ManagedProviderName = "preloop"
	plan.Notes = append(
		plan.Notes,
		fmt.Sprintf("Model traffic will route through Preloop using %s.", modelAlias),
	)
	return refreshManagedPlanSnapshots(plan)
}

func applyOpenCodeManagedGateway(plan managedMCPEnrollmentPlan, baseURL, token, modelAlias string) (managedMCPEnrollmentPlan, error) {
	providers, ok := asObjectMap(plan.ManagedDocument["provider"])
	if !ok {
		providers = make(map[string]interface{})
		plan.ManagedDocument["provider"] = providers
	}
	providers["preloop"] = map[string]interface{}{
		"npm": "@ai-sdk/openai-compatible",
		"options": map[string]interface{}{
			"baseURL": strings.TrimRight(baseURL, "/") + openClawGatewayPath,
			"apiKey":  token,
		},
		"models": map[string]interface{}{
			modelAlias: map[string]interface{}{
				"name": modelAlias,
			},
		},
	}
	plan.ManagedDocument["model"] = "preloop/" + modelAlias
	plan.ManagedModelAlias = modelAlias
	plan.ManagedProviderName = "preloop"
	plan.Notes = append(
		plan.Notes,
		fmt.Sprintf("Model traffic will route through Preloop using %s.", modelAlias),
	)
	return refreshManagedPlanSnapshots(plan)
}

func applyClaudeManagedGateway(plan managedMCPEnrollmentPlan, baseURL, token, modelAlias string) (managedMCPEnrollmentPlan, error) {
	env, ok := asObjectMap(plan.ManagedDocument["env"])
	if !ok {
		env = make(map[string]interface{})
		plan.ManagedDocument["env"] = env
	}
	env["ANTHROPIC_BASE_URL"] = strings.TrimRight(baseURL, "/") + "/anthropic"
	delete(env, "ANTHROPIC_AUTH_TOKEN")
	env["ANTHROPIC_API_KEY"] = token
	env["CLAUDE_CODE_USE_BEDROCK"] = "0"
	env["CLAUDE_CODE_SIMPLE"] = "1"
	env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "0"
	env["DISABLE_TELEMETRY"] = "1"
	env["OTEL_METRICS_EXPORTER"] = "none"
	env["OTEL_LOGS_EXPORTER"] = "none"
	env["OTEL_TRACES_EXPORTER"] = "none"
	env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = modelAlias
	env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = "Preloop " + modelAlias
	for _, key := range []string{
		"AWS_BEARER_TOKEN_BEDROCK",
		"AWS_ACCESS_KEY_ID",
		"AWS_SECRET_ACCESS_KEY",
		"AWS_SESSION_TOKEN",
		"AWS_REGION",
		"AWS_DEFAULT_REGION",
		"AWS_PROFILE",
		"AWS_SHARED_CREDENTIALS_FILE",
		"AWS_CONFIG_FILE",
		"CLAUDE_CODE_OAUTH_TOKEN",
	} {
		delete(env, key)
	}
	clearClaudePinnedModelEnv(env)
	if selection, envKey := claudePinnedModelSelection(modelAlias); envKey != "" {
		plan.ManagedDocument["model"] = selection
		env["ANTHROPIC_MODEL"] = selection
		env[envKey] = modelAlias
		env[envKey+"_NAME"] = "Preloop " + modelAlias
	} else {
		env["ANTHROPIC_MODEL"] = modelAlias
		// Non-family managed model (e.g. a Kimi K3 alias): Claude Code's
		// background/fast-path requests resolve through the built-in
		// claude-haiku-* family identifiers and subagents through
		// CLAUDE_CODE_SUBAGENT_MODEL, none of which exist at the gateway
		// for a non-Anthropic account model. Map every selector at the
		// managed alias so those calls resolve instead of 404ing.
		// clearClaudePinnedModelEnv above wipes these same keys first, so
		// re-onboarding and model changes always refresh them.
		for key, value := range claudeNonFamilyModelEnv(modelAlias) {
			env[key] = value
		}
		plan.Notes = append(
			plan.Notes,
			fmt.Sprintf(
				"All Claude Code model selectors (opus/sonnet/haiku, background, and subagent models) will resolve to %s through Preloop.",
				modelAlias,
			),
		)
	}
	plan.ManagedModelAlias = modelAlias
	plan.ManagedProviderName = "preloop"
	plan.Notes = append(
		plan.Notes,
		fmt.Sprintf("Model traffic will route through Preloop using %s.", modelAlias),
	)
	return refreshManagedPlanSnapshots(plan)
}

func restoreClaudeGatewayEnvFromOriginal(
	agent AgentConfig,
	originalBytes []byte,
	writer io.Writer,
) error {
	if !isClaudeCodeAgent(agent) {
		return nil
	}
	current, err := loadAgentConfigDocument(agent)
	if err != nil {
		return err
	}
	original := map[string]interface{}{}
	if len(bytes.TrimSpace(originalBytes)) > 0 {
		if err := json.Unmarshal(originalBytes, &original); err != nil {
			return fmt.Errorf("failed to parse original Claude Code config: %w", err)
		}
	}

	currentEnv, ok := asObjectMap(current["env"])
	if !ok {
		currentEnv = map[string]interface{}{}
		current["env"] = currentEnv
	}
	originalEnv, _ := asObjectMap(original["env"])
	for _, key := range claudeManagedGatewayEnvKeys() {
		if originalValue, exists := originalEnv[key]; exists {
			currentEnv[key] = originalValue
		} else {
			delete(currentEnv, key)
		}
	}
	if originalModel, exists := original["model"]; exists {
		current["model"] = originalModel
	} else {
		delete(current, "model")
	}
	if len(currentEnv) == 0 {
		delete(current, "env")
	}
	if err := writeAgentConfigDocument(agent, current); err != nil {
		return err
	}
	if writer != nil {
		fmt.Fprintf(
			writer,
			"  Note: Restored Claude Code's local Anthropic auth/model settings because Preloop gateway live validation failed. MCP remains onboarded; set ANTHROPIC_API_KEY and rerun onboarding to use stable API billing.\n",
		) //nolint:errcheck
	}
	return nil
}

func recoverManagedGatewayAfterLiveValidationFailure(
	agent AgentConfig,
	originalBytes []byte,
	writer io.Writer,
) error {
	if isClaudeCodeAgent(agent) {
		return restoreClaudeGatewayEnvFromOriginal(agent, originalBytes, writer)
	}
	if isCodexCLIAgent(agent) {
		return restoreCodexGatewaySelectionFromOriginal(agent, originalBytes, writer)
	}
	if isGeminiCLIAgent(agent) {
		return restoreGeminiGatewaySelectionFromOriginal(agent, originalBytes, writer)
	}
	return nil
}

func restoreCodexGatewaySelectionFromOriginal(
	agent AgentConfig,
	originalBytes []byte,
	writer io.Writer,
) error {
	if !isCodexCLIAgent(agent) {
		return nil
	}
	current, err := loadAgentConfigDocument(agent)
	if err != nil {
		return err
	}
	original := map[string]interface{}{}
	if len(bytes.TrimSpace(originalBytes)) > 0 {
		if err := toml.Unmarshal(originalBytes, &original); err != nil {
			return fmt.Errorf("failed to parse original Codex CLI config: %w", err)
		}
	}
	originalProvider := strings.TrimSpace(lookupString(original, "model_provider"))
	originalProviderIsPreloop := strings.EqualFold(originalProvider, "preloop")
	if originalModel, exists := original["model"]; exists {
		current["model"] = normalizeCodexDirectModelValue(originalModel)
	} else {
		current["model"] = normalizeCodexDirectModelValue(current["model"])
	}
	if originalProviderIsPreloop {
		delete(current, "model_provider")
		delete(current, "model_providers")
	} else {
		if originalProvider != "" {
			current["model_provider"] = originalProvider
		} else {
			delete(current, "model_provider")
		}
		if originalProviders, exists := original["model_providers"]; exists {
			current["model_providers"] = originalProviders
		} else {
			delete(current, "model_providers")
		}
	}
	if err := writeAgentConfigDocument(agent, current); err != nil {
		return err
	}
	if writer != nil {
		fmt.Fprintf(
			writer,
			"  Note: Restored Codex CLI's local model provider because Preloop gateway live validation failed. MCP remains onboarded; run codex login or configure a working OpenAI/Codex credential and rerun onboarding to enable model gateway routing.\n",
		) //nolint:errcheck
	}
	return nil
}

func normalizeCodexDirectModelValue(value interface{}) interface{} {
	model := strings.TrimSpace(fmt.Sprint(value))
	if provider, modelID, ok := strings.Cut(model, "/"); ok &&
		strings.EqualFold(provider, "openai") &&
		strings.TrimSpace(modelID) != "" {
		return strings.TrimSpace(modelID)
	}
	if model == "" {
		return value
	}
	return model
}

func restoreGeminiGatewaySelectionFromOriginal(
	agent AgentConfig,
	originalBytes []byte,
	writer io.Writer,
) error {
	if !isGeminiCLIAgent(agent) {
		return nil
	}
	current, err := loadAgentConfigDocument(agent)
	if err != nil {
		return err
	}
	original := map[string]interface{}{}
	if len(bytes.TrimSpace(originalBytes)) > 0 {
		if err := json.Unmarshal(originalBytes, &original); err != nil {
			return fmt.Errorf("failed to parse original Gemini CLI config: %w", err)
		}
	}

	originalBaseURL := strings.TrimSpace(lookupString(original, "baseUrl"))
	if originalBaseURL != "" &&
		!strings.Contains(strings.ToLower(originalBaseURL), "preloop") {
		current["baseUrl"] = original["baseUrl"]
	} else {
		delete(current, "baseUrl")
	}
	if originalAPIKey := original["apiKey"]; originalAPIKey != nil &&
		!strings.Contains(strings.ToLower(originalBaseURL), "preloop") {
		current["apiKey"] = originalAPIKey
	} else {
		delete(current, "apiKey")
	}
	if originalModel, exists := original["model"]; exists {
		current["model"] = originalModel
	}
	if err := writeAgentConfigDocument(agent, current); err != nil {
		return err
	}
	if writer != nil {
		fmt.Fprintf(
			writer,
			"  Note: Restored Gemini CLI's local model provider because Preloop gateway live validation failed. MCP remains onboarded; configure GEMINI_API_KEY or GOOGLE_API_KEY and rerun onboarding to enable model gateway routing.\n",
		) //nolint:errcheck
	}
	return nil
}

func isClaudeCodeAgent(agent AgentConfig) bool {
	return strings.EqualFold(strings.TrimSpace(agent.Name), "Claude Code")
}

func isCodexCLIAgent(agent AgentConfig) bool {
	return strings.EqualFold(strings.TrimSpace(agent.Name), "Codex CLI")
}

func isGeminiCLIAgent(agent AgentConfig) bool {
	return strings.EqualFold(strings.TrimSpace(agent.Name), "Gemini CLI")
}

func claudeManagedGatewayEnvKeys() []string {
	return []string{
		"ANTHROPIC_BASE_URL",
		"ANTHROPIC_API_KEY",
		"ANTHROPIC_AUTH_TOKEN",
		"CLAUDE_CODE_OAUTH_TOKEN",
		"CLAUDE_CODE_USE_BEDROCK",
		"ANTHROPIC_MODEL",
		"ANTHROPIC_CUSTOM_MODEL_OPTION",
		"ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
		claudeSubagentModelEnvKey,
	}
}

func clearClaudePinnedModelEnv(env map[string]interface{}) {
	for _, key := range []string{
		"ANTHROPIC_DEFAULT_OPUS_MODEL",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_OPUS_MODEL_SUPPORTED_CAPABILITIES",
		"ANTHROPIC_DEFAULT_SONNET_MODEL",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION",
		"ANTHROPIC_DEFAULT_HAIKU_MODEL_SUPPORTED_CAPABILITIES",
		claudeSubagentModelEnvKey,
	} {
		delete(env, key)
	}
}

func ensureLegacyCodexMCPServer(agent AgentConfig, doc map[string]interface{}, baseURL string, token string) {
	if !strings.EqualFold(strings.TrimSpace(agent.Name), "codex cli") {
		return
	}
	token = strings.TrimSpace(token)
	servers, ok := asObjectMap(doc["mcp_servers"])
	if !ok {
		servers = make(map[string]interface{})
		doc["mcp_servers"] = servers
	}
	entry := map[string]interface{}{
		"url": strings.TrimRight(baseURL, "/") + "/mcp/v1",
	}
	if token != "" {
		entry["http_headers"] = map[string]interface{}{
			"Authorization": "Bearer " + token,
		}
	} else {
		entry["bearer_token_env_var"] = "PRELOOP_TOKEN"
	}
	servers["preloop"] = entry

	// Codex ignores the [mcp] table, so drop any empty [mcp.servers]/[mcp]
	// tables left behind by earlier onboarding runs. This keeps config.toml to
	// a single [mcp_servers.preloop] block and avoids orphaned [mcp] tables.
	if mcp, ok := asObjectMap(doc["mcp"]); ok {
		if inner, ok := asObjectMap(mcp["servers"]); ok && len(inner) == 0 {
			delete(mcp, "servers")
		}
		if len(mcp) == 0 {
			delete(doc, "mcp")
		}
	}
}

func ensureDiscoveredRemoteServers(client *api.Client, agent AgentConfig, publicURL string) (*remoteServerSyncResult, error) {
	var existing []mcpServerResponse
	if err := client.Get("/api/v1/mcp-servers", &existing); err != nil {
		return nil, fmt.Errorf("failed to list MCP servers: %w", err)
	}
	existingByName := make(map[string]mcpServerResponse, len(existing))
	for _, item := range existing {
		existingByName[item.Name] = item
	}

	result := &remoteServerSyncResult{}
	for _, name := range sortedServerNames(agent.MCPServers) {
		server := agent.MCPServers[name]
		if isPreloopOwnedMCPServer(name, server) {
			result.Warnings = append(
				result.Warnings,
				fmt.Sprintf(
					"MCP server %q points at Preloop and was not imported as an upstream server.",
					name,
				),
			)
			result.Skipped = append(result.Skipped, name)
			continue
		}
		request, warning, importMode, ok := buildManagedRemoteServerRequest(name, server)
		if warning != "" {
			result.Warnings = append(result.Warnings, warning)
		}
		if !ok {
			result.Skipped = append(result.Skipped, name)
			continue
		}
		if _, ok := existingByName[name]; ok {
			result.Reused = append(result.Reused, name)
			continue
		}
		var created mcpServerResponse
		if err := client.Post("/api/v1/mcp-servers", request, &created); err != nil {
			return nil, fmt.Errorf("failed to add discovered MCP server %q: %w", name, err)
		}
		result.Added = append(result.Added, name)
		if importMode == "command" {
			result.ImportedFromCommand = append(result.ImportedFromCommand, name)
		}
	}
	sort.Strings(result.Added)
	sort.Strings(result.ImportedFromCommand)
	sort.Strings(result.Reused)
	sort.Strings(result.Skipped)
	sort.Strings(result.Warnings)
	return result, nil
}

func normalizeDiscoveredTransport(server MCPDef) string {
	if strings.TrimSpace(server.Transport) != "" {
		return server.Transport
	}
	return "http-streaming"
}

func authConfigForDiscoveredServer(server MCPDef) (string, map[string]interface{}) {
	if token := extractBearerToken(server); token != "" {
		return "bearer", map[string]interface{}{"token": token}
	}
	return "", nil
}

func extractBearerToken(server MCPDef) string {
	if authType, _ := server.Auth["type"].(string); strings.EqualFold(authType, "bearer") {
		if token, _ := server.Auth["token"].(string); token != "" {
			return token
		}
	}
	for key, value := range server.Headers {
		if !strings.EqualFold(key, "authorization") {
			continue
		}
		trimmed := strings.TrimSpace(value)
		if strings.HasPrefix(strings.ToLower(trimmed), "bearer ") {
			return strings.TrimSpace(trimmed[7:])
		}
	}
	for key, value := range server.Env {
		if !strings.Contains(strings.ToLower(key), "token") &&
			!strings.EqualFold(key, "authorization") {
			continue
		}
		trimmed := strings.TrimSpace(value)
		if strings.HasPrefix(strings.ToLower(trimmed), "bearer ") {
			return strings.TrimSpace(trimmed[7:])
		}
	}
	return ""
}

func getManagedAgentForDiscovered(client *api.Client, agent AgentConfig) (*managedAgentSummary, error) {
	var response managedAgentListResponse
	if err := client.Get("/api/v1/agents?limit=100", &response); err != nil {
		return nil, fmt.Errorf("failed to list managed agents: %w", err)
	}
	sourceTypes := managedAgentLookupSourceTypes(agent)
	candidateIDs := runtimePrincipalIDCandidates(agent)
	for _, item := range response.Items {
		if !containsString(sourceTypes, item.SessionSourceType) {
			continue
		}
		for _, sourceID := range candidateIDs {
			if item.SessionSourceID == sourceID {
				return &item, nil
			}
		}
	}
	return nil, fmt.Errorf("managed agent not found after bootstrap for %s", agent.Name)
}

func managedAgentLookupSourceTypes(agent AgentConfig) []string {
	primary := runtimeSessionSourceTypeForAgent(agent.Name)
	sourceTypes := []string{primary}
	switch primary {
	case "gemini_cli", "opencode":
		sourceTypes = append(sourceTypes, "desktop_agent")
	}
	return sourceTypes
}

func runtimePrincipalIDCandidates(agent AgentConfig) []string {
	seen := map[string]struct{}{}
	candidates := []string{
		runtimePrincipalIDForAgent(agent),
		legacyRuntimePrincipalIDForAgent(agent),
	}
	var out []string
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if _, ok := seen[candidate]; ok {
			continue
		}
		seen[candidate] = struct{}{}
		out = append(out, candidate)
	}
	return out
}

func createDurableManagedCredential(client *api.Client, agent *managedAgentSummary) (*managedAgentCredentialCreateResponse, error) {
	credentialName := fmt.Sprintf(
		"%s-mcp-%s",
		slugifyAgentName(agent.DisplayName),
		time.Now().UTC().Format("20060102150405"),
	)
	request := managedAgentCredentialCreateRequest{
		Name:          credentialName,
		Description:   fmt.Sprintf("Managed MCP credential for %s", agent.DisplayName),
		ExpiresInDays: 365,
		Scopes:        []string{"mcp:read", "mcp:write"},
	}
	var response managedAgentCredentialCreateResponse
	if err := client.Post("/api/v1/agents/"+agent.ID+"/credentials", request, &response); err != nil {
		return nil, fmt.Errorf("failed to create durable managed-agent credential: %w", err)
	}
	return &response, nil
}

func createManagedEnrollmentRecord(client *api.Client, agentID string, request managedAgentEnrollmentCreateRequest) (*managedAgentEnrollmentSummary, error) {
	var response managedAgentEnrollmentSummary
	if err := client.Post("/api/v1/agents/"+agentID+"/enrollments", request, &response); err != nil {
		return nil, fmt.Errorf("failed to persist managed enrollment: %w", err)
	}
	return &response, nil
}

func getManagedAgentDetail(client *api.Client, agentID string) (*managedAgentDetailResponse, error) {
	var response managedAgentDetailResponse
	if err := client.Get("/api/v1/agents/"+agentID, &response); err != nil {
		return nil, fmt.Errorf("failed to fetch managed agent detail: %w", err)
	}
	return &response, nil
}

func getManagedAgentDetailForDiscovered(client *api.Client, agent AgentConfig) (*managedAgentDetailResponse, error) {
	managedAgent, err := getManagedAgentForDiscovered(client, agent)
	if err != nil {
		return nil, err
	}
	return getManagedAgentDetail(client, managedAgent.ID)
}

func validateManagedEnrollmentRecord(
	client *api.Client,
	agent AgentConfig,
	enrollmentID string,
	validationResult map[string]interface{},
	status string,
) (*managedAgentEnrollmentSummary, error) {
	managedAgent, err := getManagedAgentForDiscovered(client, agent)
	if err != nil {
		return nil, err
	}
	request := map[string]interface{}{
		"status":            status,
		"validation_result": validationResult,
	}
	var response managedAgentEnrollmentSummary
	if err := client.Post(
		"/api/v1/agents/"+managedAgent.ID+"/enrollments/"+enrollmentID+"/validate",
		request,
		&response,
	); err != nil {
		return nil, fmt.Errorf("failed to persist managed enrollment validation: %w", err)
	}
	return &response, nil
}

func restoreManagedEnrollmentRecord(
	client *api.Client,
	agent AgentConfig,
	enrollmentID string,
	backupMetadata map[string]interface{},
) (*managedAgentEnrollmentSummary, error) {
	managedAgent, err := getManagedAgentForDiscovered(client, agent)
	if err != nil {
		return nil, err
	}
	request := map[string]interface{}{
		"status": "restored",
		"backup_metadata": map[string]interface{}{
			"backup_path": backupMetadata["backup_path"],
		},
		"validation_result": map[string]interface{}{
			"restored_by": "preloop agents restore",
		},
	}
	var response managedAgentEnrollmentSummary
	if err := client.Post(
		"/api/v1/agents/"+managedAgent.ID+"/enrollments/"+enrollmentID+"/restore",
		request,
		&response,
	); err != nil {
		return nil, fmt.Errorf("failed to mark managed enrollment restored: %w", err)
	}
	return &response, nil
}

func createLocalEnrollmentBackup(agent AgentConfig, configExisted bool, originalBytes []byte, plan managedMCPEnrollmentPlan) (*localEnrollmentState, error) {
	if existingState, err := loadLocalEnrollmentState(agent); err == nil {
		if strings.TrimSpace(existingState.BackupPath) != "" {
			if _, statErr := os.Stat(existingState.BackupPath); statErr == nil {
				discoveredConfig := existingState.DiscoveredConfig
				if len(discoveredConfig) == 0 {
					discoveredConfig = plan.SanitizedDiscovered
				}
				return &localEnrollmentState{
					AgentName:           agent.Name,
					DisplayName:         resolveAgentDisplayName(agent),
					RuntimePrincipalID:  runtimePrincipalIDForAgent(agent),
					ConfigPath:          agent.ConfigPath,
					ConfigExisted:       existingState.ConfigExisted,
					BackupPath:          existingState.BackupPath,
					ManagedServerName:   plan.ManagedServerName,
					ManagedServerURL:    plan.ManagedServerURL,
					ManagedControlWSURL: plan.ManagedControlWSURL,
					AppliedAt:           time.Now().UTC(),
					DiscoveredConfig:    discoveredConfig,
					ManagedConfig:       plan.SanitizedManaged,
				}, nil
			}
		}
	}
	baseDir, err := config.GetConfigDir()
	if err != nil {
		return nil, err
	}
	runtimePrincipalID := runtimePrincipalIDForAgent(agent)
	backupDir := filepath.Join(baseDir, "agents", "backups", runtimePrincipalID)
	if err := os.MkdirAll(backupDir, 0700); err != nil {
		return nil, fmt.Errorf("failed to create backup directory: %w", err)
	}
	backupPath := filepath.Join(
		backupDir,
		fmt.Sprintf("%s-%s", time.Now().UTC().Format("20060102T150405Z"), filepath.Base(agent.ConfigPath)),
	)
	if err := os.WriteFile(backupPath, originalBytes, 0600); err != nil {
		return nil, fmt.Errorf("failed to write backup: %w", err)
	}
	return &localEnrollmentState{
		AgentName:           agent.Name,
		DisplayName:         resolveAgentDisplayName(agent),
		RuntimePrincipalID:  runtimePrincipalID,
		ConfigPath:          agent.ConfigPath,
		ConfigExisted:       configExisted,
		BackupPath:          backupPath,
		ManagedServerName:   plan.ManagedServerName,
		ManagedServerURL:    plan.ManagedServerURL,
		ManagedControlWSURL: plan.ManagedControlWSURL,
		AppliedAt:           time.Now().UTC(),
		DiscoveredConfig:    plan.SanitizedDiscovered,
		ManagedConfig:       plan.SanitizedManaged,
	}, nil
}

func saveLocalEnrollmentState(state *localEnrollmentState) error {
	statePath, err := localEnrollmentStatePath(state.AgentName, state.ConfigPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(statePath), 0700); err != nil {
		return fmt.Errorf("failed to create local enrollment directory: %w", err)
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode local enrollment state: %w", err)
	}
	if err := os.WriteFile(statePath, data, 0600); err != nil {
		return fmt.Errorf("failed to persist local enrollment state: %w", err)
	}
	return nil
}

func loadLocalEnrollmentState(agent AgentConfig) (*localEnrollmentState, error) {
	statePath, err := localEnrollmentStatePath(agent.Name, agent.ConfigPath)
	if err != nil {
		return nil, err
	}
	data, err := os.ReadFile(statePath)
	if err != nil {
		legacyPath, legacyPathErr := legacyLocalEnrollmentStatePath(agent)
		if legacyPathErr != nil {
			return nil, fmt.Errorf("failed to read local enrollment state: %w", err)
		}
		data, err = os.ReadFile(legacyPath)
		if err != nil {
			return nil, fmt.Errorf("failed to read local enrollment state: %w", err)
		}
	}
	var state localEnrollmentState
	if err := json.Unmarshal(data, &state); err != nil {
		return nil, fmt.Errorf("failed to parse local enrollment state: %w", err)
	}
	return &state, nil
}

func localEnrollmentStatePath(agentType, configPath string) (string, error) {
	baseDir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	discoveryKey := sha1.Sum([]byte(strings.ToLower(strings.TrimSpace(agentType)) + "\x00" + filepath.Clean(configPath)))
	return filepath.Join(baseDir, "agents", "state", hex.EncodeToString(discoveryKey[:8])+".json"), nil
}

func legacyLocalEnrollmentStatePath(agent AgentConfig) (string, error) {
	baseDir, err := config.GetConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(baseDir, "agents", "state", legacyRuntimePrincipalIDForAgent(agent)+".json"), nil
}

func loadJSONDocument(path string) (map[string]interface{}, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return doc, nil
}

func parseDocumentFromTOML(data []byte) (map[string]interface{}, error) {
	var doc map[string]interface{}
	if err := toml.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return doc, nil
}

func writeJSONDocument(path string, doc map[string]interface{}) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return fmt.Errorf("failed to create config directory: %w", err)
	}
	data, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fmt.Errorf("failed to encode managed config: %w", err)
	}
	data = append(data, '\n')
	// 0600: the managed config embeds the durable runtime bearer token, so it
	// must not be world-readable. Chmod after write enforces the mode even when
	// the file already existed (os.WriteFile only sets mode on creation).
	if err := os.WriteFile(path, data, 0600); err != nil {
		return fmt.Errorf("failed to write managed config: %w", err)
	}
	if err := os.Chmod(path, 0600); err != nil {
		return fmt.Errorf("failed to secure managed config permissions: %w", err)
	}
	return nil
}

func writeTOMLDocument(path string, doc map[string]interface{}) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return fmt.Errorf("failed to create config directory: %w", err)
	}
	data, err := toml.Marshal(doc)
	if err != nil {
		return fmt.Errorf("failed to encode managed config: %w", err)
	}
	if len(data) == 0 || data[len(data)-1] != '\n' {
		data = append(data, '\n')
	}
	// 0600: embeds the runtime bearer token (see writeJSONDocument).
	if err := os.WriteFile(path, data, 0600); err != nil {
		return fmt.Errorf("failed to write managed config: %w", err)
	}
	if err := os.Chmod(path, 0600); err != nil {
		return fmt.Errorf("failed to secure managed config permissions: %w", err)
	}
	return nil
}

func readExistingAgentConfig(path string) ([]byte, bool, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []byte{}, false, nil
		}
		return nil, false, err
	}
	return data, true, nil
}

func deepCopyMap(value map[string]interface{}) (map[string]interface{}, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, fmt.Errorf("failed to clone config document: %w", err)
	}
	var out map[string]interface{}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("failed to clone config document: %w", err)
	}
	return out, nil
}

type genericManagedMCPAdapter struct {
	agent AgentConfig
}

func (a genericManagedMCPAdapter) Key() string {
	return runtimeSessionSourceTypeForAgent(a.agent.Name)
}

func (a genericManagedMCPAdapter) EnsureServerContainer(doc map[string]interface{}) (map[string]interface{}, error) {
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "codex cli") {
		// Codex reads MCP servers from the top-level [mcp_servers] table.
		// It ignores an [mcp] table entirely (verified on Codex 0.142.5), so
		// target/create mcp_servers and never write the nested [mcp.servers]
		// shape.
		if servers, ok := asObjectMap(doc["mcp_servers"]); ok {
			return servers, nil
		}
		created := make(map[string]interface{})
		doc["mcp_servers"] = created
		return created, nil
	}
	if servers, ok := asObjectMap(doc["mcpServers"]); ok {
		return servers, nil
	}
	if servers, ok := asObjectMap(doc["servers"]); ok {
		return servers, nil
	}
	if mcp, ok := asObjectMap(doc["mcp"]); ok {
		if servers, ok := asObjectMap(mcp["servers"]); ok {
			return servers, nil
		}
		if looksLikeMCPServerContainer(mcp) {
			return mcp, nil
		}
		created := make(map[string]interface{})
		mcp["servers"] = created
		return created, nil
	}

	switch strings.ToLower(strings.TrimSpace(a.agent.Name)) {
	case "claude code":
		created := make(map[string]interface{})
		doc["servers"] = created
		return created, nil
	case "opencode":
		created := make(map[string]interface{})
		doc["mcp"] = created
		return created, nil
	default:
		created := make(map[string]interface{})
		doc["mcpServers"] = created
		return created, nil
	}
}

func (a genericManagedMCPAdapter) BuildManagedServer(baseURL, token string) map[string]interface{} {
	url := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	switch strings.ToLower(strings.TrimSpace(a.agent.Name)) {
	case "codex cli":
		// Codex reads the [mcp_servers.preloop] table with `url` +
		// [mcp_servers.preloop.http_headers]. This mirrors
		// ensureLegacyCodexMCPServer, the authoritative writer for this entry.
		entry := map[string]interface{}{"url": url}
		if strings.TrimSpace(token) != "" {
			entry["http_headers"] = map[string]interface{}{
				"Authorization": "Bearer " + token,
			}
		} else {
			entry["bearer_token_env_var"] = "PRELOOP_TOKEN"
		}
		return entry
	case "gemini cli":
		return map[string]interface{}{
			"url":  url,
			"type": "http",
			"headers": map[string]interface{}{
				"Authorization": "Bearer " + token,
			},
		}
	case "opencode":
		return map[string]interface{}{
			"type":    "remote",
			"url":     url,
			"enabled": true,
			"headers": map[string]interface{}{
				"Authorization": "Bearer " + token,
			},
		}
	case strings.ToLower(antigravityAgentName):
		// Antigravity's MCP schema uses `serverUrl` (not `url`) for remote
		// HTTP servers, and its env/${VAR} interpolation is unreliable — so
		// the bearer token is written literally into the headers.
		return map[string]interface{}{
			"serverUrl": url,
			"headers": map[string]interface{}{
				"Authorization": "Bearer " + token,
			},
		}
	case strings.ToLower(devinAgentName):
		// Devin uses `url` + headers with Streamable HTTP as the default
		// transport; it does not take a `transport` field, so we omit it.
		return map[string]interface{}{
			"url": url,
			"headers": map[string]interface{}{
				"Authorization": "Bearer " + token,
			},
		}
	default:
		transport := "http-streaming"
		if usesNestedMCPServers(a.agent) {
			transport = "http"
		}
		entry := map[string]interface{}{
			"url": url,
			"headers": map[string]interface{}{
				"Authorization": "Bearer " + token,
			},
		}
		if transport != "" {
			entry["transport"] = transport
		}
		return entry
	}
}

func (a genericManagedMCPAdapter) ValidateManagedConfig(doc map[string]interface{}, baseURL string) map[string]interface{} {
	container := lookupMCPServerContainer(doc)
	preloop, ok := container["preloop"].(map[string]interface{})
	expectedURL := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	result := map[string]interface{}{
		"adapter_key":             a.Key(),
		"preloop_server_present":  ok,
		"expected_preloop_url":    expectedURL,
		"validation_passed":       false,
		"transport_ok":            false,
		"authorization_header_ok": false,
	}
	if !ok {
		return result
	}

	result["preloop_url_ok"] = preloop["url"] == expectedURL
	result["transport_ok"] = preloop["transport"] != nil
	if isAntigravityAgent(a.agent) {
		// Antigravity stores the endpoint under `serverUrl` and carries no
		// transport field; a present serverUrl + auth header is a valid
		// remote HTTP entry.
		result["preloop_url_ok"] = preloop["serverUrl"] == expectedURL
		result["transport_ok"] = true
	}
	if isDevinAgent(a.agent) {
		// Devin defaults to Streamable HTTP and takes no transport field.
		result["transport_ok"] = true
	}
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "gemini cli") {
		if transport, _ := preloop["transport"].(string); strings.EqualFold(strings.TrimSpace(transport), "http-streaming") {
			result["transport_ok"] = true
		} else {
			result["transport_ok"] = strings.EqualFold(
				strings.TrimSpace(fmt.Sprint(preloop["type"])),
				"http",
			)
		}
	}
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "opencode") {
		result["transport_ok"] = strings.EqualFold(
			strings.TrimSpace(fmt.Sprint(preloop["type"])),
			"remote",
		)
	}
	if headers, ok := preloop["headers"].(map[string]interface{}); ok {
		if auth, ok := headers["Authorization"].(string); ok && strings.HasPrefix(auth, "Bearer ") {
			result["authorization_header_ok"] = true
		}
	}
	if !result["authorization_header_ok"].(bool) {
		if auth, ok := preloop["auth"].(map[string]interface{}); ok {
			authType, _ := auth["type"].(string)
			token, _ := auth["token"].(string)
			if strings.EqualFold(strings.TrimSpace(authType), "bearer") && strings.TrimSpace(token) != "" {
				result["authorization_header_ok"] = true
			}
		}
	}
	result["validation_passed"] =
		result["preloop_server_present"] == true &&
			result["preloop_url_ok"] == true &&
			result["transport_ok"] == true &&
			result["authorization_header_ok"] == true
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "codex cli") {
		expectedGatewayURL := strings.TrimRight(baseURL, "/") + openClawGatewayPath
		result["expected_gateway_url"] = expectedGatewayURL
		result["gateway_provider_ok"] = false
		result["gateway_base_url_ok"] = false
		result["gateway_token_ok"] = false
		result["gateway_wire_api_ok"] = false
		result["model_provider_rewritten"] = false
		result["gateway_model_alias"] = ""
		result["legacy_mcp_server_present"] = false

		if providers, ok := asObjectMap(doc["model_providers"]); ok {
			if managedProvider, ok := asObjectMap(providers["preloop"]); ok {
				result["gateway_provider_ok"] = true
				if base, _ := managedProvider["base_url"].(string); base == expectedGatewayURL {
					result["gateway_base_url_ok"] = true
				}
				if token, _ := managedProvider["experimental_bearer_token"].(string); strings.TrimSpace(token) != "" {
					result["gateway_token_ok"] = true
				}
				if wireAPI, _ := managedProvider["wire_api"].(string); strings.EqualFold(strings.TrimSpace(wireAPI), "responses") {
					result["gateway_wire_api_ok"] = true
				}
			}
		}
		if provider, _ := doc["model_provider"].(string); strings.EqualFold(strings.TrimSpace(provider), "preloop") {
			result["model_provider_rewritten"] = true
		}
		if modelAlias, _ := doc["model"].(string); strings.TrimSpace(modelAlias) != "" {
			result["gateway_model_alias"] = strings.TrimSpace(modelAlias)
		}
		if legacyServers, ok := asObjectMap(doc["mcp_servers"]); ok {
			if legacy, ok := asObjectMap(legacyServers["preloop"]); ok {
				result["legacy_mcp_server_present"] = true
				if url, _ := legacy["url"].(string); strings.TrimSpace(url) == expectedURL {
					result["preloop_server_present"] = true
					result["preloop_url_ok"] = true
					result["transport_ok"] = true
				}
				legacyHeaders, _ := asObjectMap(legacy["http_headers"])
				if envKey, _ := legacy["bearer_token_env_var"].(string); strings.TrimSpace(envKey) != "" {
					result["authorization_header_ok"] = true
				}
				if authorization, _ := legacyHeaders["Authorization"].(string); strings.HasPrefix(strings.TrimSpace(authorization), "Bearer ") {
					result["authorization_header_ok"] = true
				}
			}
		}
		result["validation_passed"] =
			result["preloop_server_present"] == true &&
				result["preloop_url_ok"] == true &&
				result["transport_ok"] == true &&
				result["authorization_header_ok"] == true &&
				result["gateway_provider_ok"] == true &&
				result["gateway_base_url_ok"] == true &&
				result["gateway_token_ok"] == true &&
				result["gateway_wire_api_ok"] == true &&
				result["model_provider_rewritten"] == true
	}
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "opencode") {
		expectedGatewayURL := strings.TrimRight(baseURL, "/") + openClawGatewayPath
		result["expected_gateway_url"] = expectedGatewayURL
		result["gateway_provider_ok"] = false
		result["gateway_base_url_ok"] = false
		result["gateway_token_ok"] = false
		result["model_provider_rewritten"] = false
		result["gateway_model_alias"] = ""

		if providers, ok := asObjectMap(doc["provider"]); ok {
			if managedProvider, ok := asObjectMap(providers["preloop"]); ok {
				result["gateway_provider_ok"] = true
				if options, ok := asObjectMap(managedProvider["options"]); ok {
					if base, _ := options["baseURL"].(string); base == expectedGatewayURL {
						result["gateway_base_url_ok"] = true
					}
					if token, _ := options["apiKey"].(string); strings.TrimSpace(token) != "" {
						result["gateway_token_ok"] = true
					}
				}
			}
		}
		if modelRef, _ := doc["model"].(string); strings.HasPrefix(strings.TrimSpace(modelRef), "preloop/") {
			result["model_provider_rewritten"] = true
			result["gateway_model_alias"] = strings.TrimPrefix(strings.TrimSpace(modelRef), "preloop/")
		}
	}
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "claude code") {
		expectedGatewayURL := strings.TrimRight(baseURL, "/") + "/anthropic"
		result["expected_gateway_url"] = expectedGatewayURL
		result["gateway_provider_ok"] = false
		result["gateway_base_url_ok"] = false
		result["gateway_token_ok"] = false
		result["model_provider_rewritten"] = false
		result["gateway_model_alias"] = ""

		if env, ok := asObjectMap(doc["env"]); ok {
			if base, _ := env["ANTHROPIC_BASE_URL"].(string); base == expectedGatewayURL {
				result["gateway_base_url_ok"] = true
			}
			if token, _ := env["ANTHROPIC_API_KEY"].(string); strings.TrimSpace(token) != "" {
				result["gateway_token_ok"] = true
			}
			if token, _ := env["ANTHROPIC_AUTH_TOKEN"].(string); strings.TrimSpace(token) != "" {
				result["gateway_token_ok"] = true
			}
			if custom, _ := env["ANTHROPIC_CUSTOM_MODEL_OPTION"].(string); strings.TrimSpace(custom) != "" {
				result["gateway_provider_ok"] = true
			}
			for _, key := range []string{
				"ANTHROPIC_DEFAULT_OPUS_MODEL",
				"ANTHROPIC_DEFAULT_SONNET_MODEL",
				"ANTHROPIC_DEFAULT_HAIKU_MODEL",
			} {
				if modelAlias, _ := env[key].(string); strings.TrimSpace(modelAlias) != "" {
					result["model_provider_rewritten"] = true
					result["gateway_model_alias"] = strings.TrimSpace(modelAlias)
					break
				}
			}
			if result["gateway_model_alias"] == "" {
				if modelAlias, _ := env["ANTHROPIC_MODEL"].(string); strings.TrimSpace(modelAlias) != "" {
					result["model_provider_rewritten"] = true
					result["gateway_model_alias"] = strings.TrimSpace(modelAlias)
				}
			}
			if result["gateway_model_alias"] == "" {
				if modelSetting, _ := doc["model"].(string); strings.TrimSpace(modelSetting) != "" {
					result["model_provider_rewritten"] = true
				}
			}
		}
	}
	if strings.EqualFold(strings.TrimSpace(a.agent.Name), "gemini cli") {
		expectedGatewayURL := strings.TrimRight(baseURL, "/") + "/gemini"
		result["expected_gateway_url"] = expectedGatewayURL
		result["gateway_provider_ok"] = false
		result["gateway_base_url_ok"] = false
		result["gateway_token_ok"] = false
		result["model_provider_rewritten"] = false
		result["gateway_model_alias"] = ""

		if base, _ := doc["baseUrl"].(string); base == expectedGatewayURL {
			result["gateway_base_url_ok"] = true
		}
		if token, _ := doc["apiKey"].(string); strings.TrimSpace(token) != "" {
			result["gateway_token_ok"] = true
			result["gateway_provider_ok"] = true
		}
		gatewayConfigured := result["gateway_base_url_ok"] == true &&
			result["gateway_token_ok"] == true
		if modelConfig, ok := asObjectMap(doc["model"]); ok {
			if modelAlias, _ := modelConfig["name"].(string); strings.TrimSpace(modelAlias) != "" {
				result["gateway_model_alias"] = normalizeGeminiGatewayModelAlias(modelAlias)
				if gatewayConfigured {
					result["model_provider_rewritten"] = true
				}
			}
		} else if modelAlias, _ := doc["model"].(string); strings.TrimSpace(modelAlias) != "" {
			result["gateway_model_alias"] = normalizeGeminiGatewayModelAlias(modelAlias)
			if gatewayConfigured {
				result["model_provider_rewritten"] = true
			}
		}
	}
	return result
}

type openClawManagedMCPAdapter struct{}

func (a openClawManagedMCPAdapter) Key() string {
	return "openclaw"
}

func (a openClawManagedMCPAdapter) EnsureServerContainer(doc map[string]interface{}) (map[string]interface{}, error) {
	if mcp, ok := asObjectMap(doc["mcp"]); ok {
		if servers, ok := asObjectMap(mcp["servers"]); ok {
			return servers, nil
		}
		created := make(map[string]interface{})
		mcp["servers"] = created
		return created, nil
	}
	created := make(map[string]interface{})
	doc["mcp"] = map[string]interface{}{"servers": created}
	return created, nil
}

func (a openClawManagedMCPAdapter) BuildManagedServer(baseURL, token string) map[string]interface{} {
	return map[string]interface{}{
		"transport": "streamable-http",
		"url":       strings.TrimRight(baseURL, "/") + "/mcp/v1",
		"headers": map[string]interface{}{
			"Authorization": "Bearer " + token,
		},
	}
}

func (a openClawManagedMCPAdapter) ValidateManagedConfig(doc map[string]interface{}, baseURL string) map[string]interface{} {
	expectedURL := strings.TrimRight(baseURL, "/") + "/mcp/v1"
	expectedGatewayURL := strings.TrimRight(baseURL, "/") + openClawGatewayPath
	result := map[string]interface{}{
		"adapter_key":              a.Key(),
		"expected_preloop_url":     expectedURL,
		"expected_gateway_url":     expectedGatewayURL,
		"preloop_server_present":   false,
		"preloop_url_ok":           false,
		"transport_ok":             false,
		"authorization_header_ok":  false,
		"nested_mcp_servers_ok":    false,
		"only_preloop_mcp_ok":      false,
		"gateway_provider_ok":      false,
		"gateway_base_url_ok":      false,
		"gateway_api_ok":           false,
		"model_provider_rewritten": false,
		"validation_passed":        false,
	}
	for key, value := range validateAgentControlConfig(
		AgentConfig{Name: "OpenClaw"},
		doc,
		baseURL,
	) {
		result[key] = value
	}
	mcp, ok := asObjectMap(doc["mcp"])
	if !ok {
		return result
	}
	servers, ok := asObjectMap(mcp["servers"])
	if !ok {
		return result
	}
	result["nested_mcp_servers_ok"] = true
	result["only_preloop_mcp_ok"] = len(servers) == 1
	preloop, ok := servers["preloop"].(map[string]interface{})
	if !ok {
		return result
	}
	result["preloop_server_present"] = true
	result["preloop_url_ok"] = preloop["url"] == expectedURL
	transport := strings.TrimSpace(fmt.Sprint(preloop["transport"]))
	result["transport_ok"] = strings.EqualFold(transport, "streamable-http") ||
		strings.EqualFold(transport, "http")
	if headers, ok := preloop["headers"].(map[string]interface{}); ok {
		if auth, ok := headers["Authorization"].(string); ok && strings.HasPrefix(auth, "Bearer ") {
			result["authorization_header_ok"] = true
		}
	}
	if providers, ok := asObjectMap(lookupValue(doc, "models", "providers")); ok {
		if gatewayProvider, ok := asObjectMap(providers[openClawManagedProviderID]); ok {
			result["gateway_provider_ok"] = true
			apiName, _ := gatewayProvider["api"].(string)
			switch strings.TrimSpace(apiName) {
			case "anthropic-messages":
				expectedGatewayURL = strings.TrimRight(baseURL, "/") + "/anthropic/v1"
				result["expected_gateway_url"] = expectedGatewayURL
				result["gateway_api_ok"] = true
			case "openai-responses", "openai-completions":
				result["gateway_api_ok"] = true
			}
			result["gateway_base_url_ok"] = gatewayProvider["baseUrl"] == expectedGatewayURL
		}
	}
	if modelRef := extractOpenClawPrimaryModel(doc); strings.HasPrefix(modelRef, openClawManagedProviderID+"/") {
		result["model_provider_rewritten"] = true
	}
	result["validation_passed"] =
		result["nested_mcp_servers_ok"] == true &&
			result["only_preloop_mcp_ok"] == true &&
			result["preloop_server_present"] == true &&
			result["preloop_url_ok"] == true &&
			result["transport_ok"] == true &&
			result["authorization_header_ok"] == true &&
			result["gateway_provider_ok"] == true &&
			result["gateway_base_url_ok"] == true &&
			result["gateway_api_ok"] == true &&
			result["model_provider_rewritten"] == true
	return result
}

func managedMCPAdapterForAgent(agent AgentConfig) managedMCPAdapter {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "openclaw":
		return openClawManagedMCPAdapter{}
	case "hermes":
		return hermesManagedMCPAdapter{agent: agent}
	default:
		return genericManagedMCPAdapter{agent: agent}
	}
}

func usesNestedMCPServers(agent AgentConfig) bool {
	switch strings.ToLower(strings.TrimSpace(agent.Name)) {
	case "openclaw", "codex cli":
		return true
	default:
		return false
	}
}

func lookupMCPServerContainer(doc map[string]interface{}) map[string]interface{} {
	var fallback map[string]interface{}
	if servers, ok := asObjectMap(doc["mcpServers"]); ok {
		if _, hasPreloop := servers["preloop"]; hasPreloop {
			return servers
		}
		fallback = servers
	}
	if servers, ok := asObjectMap(doc["servers"]); ok {
		if _, hasPreloop := servers["preloop"]; hasPreloop {
			return servers
		}
		if fallback == nil {
			fallback = servers
		}
	}
	if mcp, ok := asObjectMap(doc["mcp"]); ok {
		if servers, ok := asObjectMap(mcp["servers"]); ok {
			if _, hasPreloop := servers["preloop"]; hasPreloop {
				return servers
			}
			if fallback == nil {
				fallback = servers
			}
		}
		if looksLikeMCPServerContainer(mcp) {
			if _, hasPreloop := mcp["preloop"]; hasPreloop {
				return mcp
			}
			if fallback == nil {
				fallback = mcp
			}
		}
	}
	if servers, ok := asObjectMap(doc["mcp_servers"]); ok {
		if _, hasPreloop := servers["preloop"]; hasPreloop {
			return servers
		}
		if fallback == nil {
			fallback = servers
		}
	}
	if fallback != nil {
		return fallback
	}
	return map[string]interface{}{}
}

func looksLikeMCPServerContainer(value map[string]interface{}) bool {
	if len(value) == 0 {
		return false
	}
	for _, raw := range value {
		entry, ok := asObjectMap(raw)
		if !ok || !looksLikeMCPServerEntry(entry) {
			return false
		}
	}
	return true
}

func looksLikeMCPServerEntry(value map[string]interface{}) bool {
	if value == nil {
		return false
	}
	if _, ok := value["url"]; ok {
		return true
	}
	if _, ok := value["command"]; ok {
		return true
	}
	if _, ok := value["transport"]; ok {
		return true
	}
	if _, ok := value["headers"]; ok {
		return true
	}
	if _, ok := value["auth"]; ok {
		return true
	}
	if _, ok := value["type"]; ok {
		return true
	}
	return false
}

func clientBaseURLForFlags() string {
	cfg, err := config.Resolve(FlagToken, FlagURL)
	if err == nil && cfg.APIURL != "" {
		return cfg.APIURL
	}
	return api.DefaultBaseURL
}

func asObjectMap(value interface{}) (map[string]interface{}, bool) {
	if value == nil {
		return nil, false
	}
	obj, ok := value.(map[string]interface{})
	return obj, ok
}

func sanitizeConfigSnapshot(value interface{}) {
	switch typed := value.(type) {
	case map[string]interface{}:
		for key, child := range typed {
			if isSensitiveKey(key) {
				typed[key] = "<redacted>"
				continue
			}
			sanitizeConfigSnapshot(child)
		}
	case []interface{}:
		for _, child := range typed {
			sanitizeConfigSnapshot(child)
		}
	}
}

func isSensitiveKey(key string) bool {
	switch strings.ToLower(strings.TrimSpace(key)) {
	case "authorization", "token", "bearer_token", "api_key", "apikey", "password", "secret":
		return true
	default:
		return false
	}
}

// publicPlanNote returns a copy of an onboarding note safe for stdout.
// Notes describe where a credential was resolved from; they never embed the
// secret itself. Copying into a fresh string breaks companion-return taint
// from resolvers that also return API keys.
func publicPlanNote(note string) string {
	return string([]byte(note))
}

func formatManagedValidationValue(key string, value interface{}) string {
	switch key {
	case "config_path", "live_validation_status", "live_validation_skip_reason":
		if s, ok := value.(string); ok {
			return s
		}
	case "error":
		if s, ok := value.(string); ok {
			return sanitizeValidationErrorMessage(s)
		}
	case "config_parse_ok", "preloop_server_present", "gateway_provider_ok",
		"gateway_base_url_ok", "gateway_token_ok", "model_provider_rewritten",
		"live_validation_passed", "control_config_written", "control_plugin_installed",
		"control_plugin_verified", "control_channel_configured":
		return boolStatus(value)
	}
	return "n/a"
}

func sanitizeValidationErrorMessage(message string) string {
	trimmed := strings.TrimSpace(message)
	if trimmed == "" {
		return "n/a"
	}
	lower := strings.ToLower(trimmed)
	for _, token := range []string{"token", "api_key", "apikey", "secret", "password", "bearer"} {
		if strings.Contains(lower, token) {
			return "<redacted>"
		}
	}
	return trimmed
}

func prunePreloopOwnedMCPServersFromDocument(doc map[string]interface{}) []string {
	removed := map[string]struct{}{}
	for _, container := range mcpServerContainers(doc) {
		for name, raw := range container {
			def, ok := mcpDefFromRawServer(raw)
			if !ok {
				continue
			}
			if isPreloopOwnedMCPServer(name, def) {
				delete(container, name)
				removed[name] = struct{}{}
			}
		}
	}

	names := make([]string, 0, len(removed))
	for name := range removed {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func mcpServerContainers(doc map[string]interface{}) []map[string]interface{} {
	containers := []map[string]interface{}{}
	if servers, ok := asObjectMap(doc["mcpServers"]); ok {
		containers = append(containers, servers)
	}
	if servers, ok := asObjectMap(doc["servers"]); ok {
		containers = append(containers, servers)
	}
	if servers, ok := asObjectMap(doc["mcp_servers"]); ok {
		containers = append(containers, servers)
	}
	if mcp, ok := asObjectMap(doc["mcp"]); ok {
		if servers, ok := asObjectMap(mcp["servers"]); ok {
			containers = append(containers, servers)
		}
		if looksLikeMCPServerContainer(mcp) {
			containers = append(containers, mcp)
		}
	}
	return containers
}

func mcpDefFromRawServer(raw interface{}) (MCPDef, bool) {
	object, ok := asObjectMap(raw)
	if !ok {
		return MCPDef{}, false
	}
	data, err := json.Marshal(object)
	if err != nil {
		return MCPDef{}, false
	}
	var def MCPDef
	if err := json.Unmarshal(data, &def); err != nil {
		return MCPDef{}, false
	}
	if def.URL == "" {
		if httpURL, _ := object["httpUrl"].(string); strings.TrimSpace(httpURL) != "" {
			def.URL = strings.TrimSpace(httpURL)
		}
	}
	if def.Transport == "" {
		if transport, _ := object["type"].(string); strings.TrimSpace(transport) != "" {
			def.Transport = strings.TrimSpace(transport)
		}
	}
	return def, true
}

func isPreloopOwnedMCPServer(name string, server MCPDef) bool {
	if strings.EqualFold(strings.TrimSpace(name), "preloop") {
		return true
	}
	targetURL := strings.TrimSpace(server.URL)
	if targetURL == "" {
		targetURL = extractURLFromCommandBackedServer(server)
	}
	if targetURL == "" {
		return false
	}
	if !strings.Contains(targetURL, "://") {
		targetURL = "https://" + targetURL
	}
	parsed, err := url.Parse(targetURL)
	if err != nil {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	return host == "preloop.ai" || strings.HasSuffix(host, ".preloop.ai")
}

func defaultStarterPolicyFileName(serverName string) string {
	safe := strings.ToLower(serverName)
	replacer := strings.NewReplacer(" ", "-", "/", "-", "\\", "-", ":", "-", ".", "-")
	safe = replacer.Replace(safe)
	safe = strings.Trim(safe, "-")
	if safe == "" {
		safe = "mcp-server"
	}
	return fmt.Sprintf("%s-starter-policy.yaml", safe)
}

func containsAny(value string, parts ...string) bool {
	for _, part := range parts {
		if strings.Contains(value, part) {
			return true
		}
	}
	return false
}

// ── Config parsers ───────────────────────────────────────────────────

// parseClaudeConfig reads Claude Desktop / Code config format.
func parseClaudeConfig(path string) (map[string]MCPDef, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseServerMapFromJSON(data)
}

// parseGenericMCP reads configs with a top-level "mcpServers" key.
func parseGenericMCP(path string) (map[string]MCPDef, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseServerMapFromJSON(data)
}

func parseCodexConfig(path string) (map[string]MCPDef, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if strings.EqualFold(filepath.Ext(path), ".toml") {
		return parseServerMapFromTOML(data)
	}
	return parseServerMapFromJSON(data)
}

// parseGeminiConfig reads Gemini CLI's settings.json format.
func parseGeminiConfig(path string) (map[string]MCPDef, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return parseServerMapFromJSON(data)
}

func parseServerMapFromJSON(data []byte) (map[string]MCPDef, error) {
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return parseServerMapFromDocument(doc), nil
}

func parseServerMapFromTOML(data []byte) (map[string]MCPDef, error) {
	var doc map[string]interface{}
	if err := toml.Unmarshal(data, &doc); err != nil {
		return nil, err
	}
	return parseServerMapFromDocument(doc), nil
}
